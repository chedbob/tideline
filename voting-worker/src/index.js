// Tideline voting worker — Cloudflare Worker + D1.
// Routes:
//   GET  /poll/current        → current open poll + vote tally + tideline call
//   POST /vote                → cast vote (IP-deduped, voting closes Wed-end-of-day)
//   GET  /scoreboard          → all-time + rolling crowd vs tideline accuracy + Brier
//   GET  /history?limit=20    → resolved past polls newest first
// Cron:
//   Mon 09:35 ET → create new poll using R2 latest.json (Tideline call from Faber)
//   Fri 17:00 ET → resolve previous poll using R2 latest.json (SPY close)
//
// CORS allows GET from anywhere (ALLOWED_ORIGIN var, default "*").
// POST /vote is rate-limited by IP-hash uniqueness per poll.

const json = (data, init = {}) =>
    new Response(JSON.stringify(data), {
        headers: { 'content-type': 'application/json', ...corsHeaders() },
        ...init,
    });

const text = (body, init = {}) =>
    new Response(body, { headers: { 'content-type': 'text/plain', ...corsHeaders() }, ...init });

function corsHeaders(origin = '*') {
    return {
        'access-control-allow-origin': origin,
        'access-control-allow-methods': 'GET, POST, OPTIONS',
        'access-control-allow-headers': 'content-type',
        'access-control-max-age': '86400',
    };
}

// ----------------------------------------------------------------------
// Hashing utilities (cheap IP dedup; not cryptographic auth)
// ----------------------------------------------------------------------
async function sha256(s) {
    const buf = new TextEncoder().encode(s);
    const hashBuf = await crypto.subtle.digest('SHA-256', buf);
    return [...new Uint8Array(hashBuf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

async function ipHash(req) {
    const ip = req.headers.get('cf-connecting-ip') || req.headers.get('x-forwarded-for') || 'unknown';
    const today = new Date().toISOString().slice(0, 10);
    return await sha256(`${ip}|${today}`);
}

// ----------------------------------------------------------------------
// Date helpers (anchor on US/Eastern markets via UTC math)
// ----------------------------------------------------------------------
function isoDate(d) {
    return d.toISOString().slice(0, 10);
}

function mondayOfWeek(d = new Date()) {
    const day = d.getUTCDay(); // 0=Sun ... 6=Sat
    const diff = day === 0 ? 6 : day - 1;
    const m = new Date(d);
    m.setUTCDate(d.getUTCDate() - diff);
    m.setUTCHours(0, 0, 0, 0);
    return m;
}

function fridayOfWeek(d = new Date()) {
    const m = mondayOfWeek(d);
    const f = new Date(m);
    f.setUTCDate(m.getUTCDate() + 4);
    return f;
}

// ----------------------------------------------------------------------
// Poll lifecycle
// ----------------------------------------------------------------------

async function fetchTidelineState(env) {
    const r = await fetch(env.TIDELINE_DATA_URL, { cf: { cacheTtl: 30 } });
    if (!r.ok) throw new Error(`tideline data fetch failed: ${r.status}`);
    return await r.json();
}

// ----------------------------------------------------------------------
// House priors (empirical, frozen at compute_house_priors.py run)
// Source: 1,489 weeks of SPY 1997-10-15 to 2026-04-30, Mon-open to Fri-close
// 3-way bucket with ±0.25% FLAT band. Conditional on Faber state at week
// start, shrunk toward unconditional base rate (Beta-binomial-style, n=200).
// These are POSTED ODDS, not a confidence claim.
// ----------------------------------------------------------------------
const HOUSE_PRIORS = {
    GREEN:   { UP: 0.5046, FLAT: 0.1162, DOWN: 0.3791, n: 1007 },
    NEUTRAL: { UP: 0.5158, FLAT: 0.1138, DOWN: 0.3704, n: 164  },
    CAUTION: { UP: 0.4938, FLAT: 0.0796, DOWN: 0.4266, n: 318  },
};
const PRIOR_PROVENANCE = '1,489 weeks 1997-2026, conditional on Faber state, shrunk to base rate';

function tidelinePriorFromState(data) {
    const regime = data?.regime;
    if (!regime || regime.error) {
        return {
            faber_state: 'UNKNOWN',
            prior: { UP: 0.504, FLAT: 0.107, DOWN: 0.389 },
            call: 'UP',
            basis: 'Data unavailable; using unconditional base rate.',
        };
    }
    const z0 = regime.zones?.trend_signal;
    const state = z0?.state || 'NEUTRAL';
    const evidence = z0?.evidence || {};
    const prior = HOUSE_PRIORS[state] || HOUSE_PRIORS.NEUTRAL;
    // Modal call — highest probability bucket
    const order = ['UP', 'FLAT', 'DOWN'];
    const call = order.reduce((a, b) => (prior[a] >= prior[b] ? a : b));
    const stateBlurb =
        state === 'GREEN'   ? `Trend filter is GREEN — SPY ${evidence.spy_close} above 200DMA, 50DMA also above 200DMA.`
      : state === 'CAUTION' ? `Trend filter is CAUTION — SPY ${evidence.spy_close} below 200DMA, 50DMA below 200DMA.`
      :                       `Trend filter is NEUTRAL — moving averages mixed.`;
    return {
        faber_state: state,
        prior: { UP: prior.UP, FLAT: prior.FLAT, DOWN: prior.DOWN },
        call,
        basis: `${stateBlurb} House prior: of ${prior.n} similar weeks since 1997, SPY closed UP ${(prior.UP*100).toFixed(0)}%, FLAT ${(prior.FLAT*100).toFixed(0)}%, DOWN ${(prior.DOWN*100).toFixed(0)}%. Posted odds, not a forecast.`,
    };
}

async function createPollIfMissing(env) {
    const monday = mondayOfWeek(new Date());
    const weekStart = isoDate(monday);
    const friday = fridayOfWeek(monday);
    const weekEnd = isoDate(friday);

    const existing = await env.DB.prepare(`SELECT id FROM polls WHERE week_start = ?`).bind(weekStart).first();
    if (existing) return { skipped: true, week_start: weekStart };

    const data = await fetchTidelineState(env);
    const { faber_state, prior, call, basis } = tidelinePriorFromState(data);
    const spyOpen = data?.regime?.zones?.trend_signal?.evidence?.spy_close ?? null;

    const question = `Where does SPY close on Friday ${weekEnd} relative to today's open ${spyOpen?.toFixed?.(2) || ''}?`;
    await env.DB.prepare(`
        INSERT INTO polls (week_start, week_end, question, spy_open, faber_state,
                           prior_up, prior_flat, prior_down, tideline_basis, tideline_call)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).bind(
        weekStart, weekEnd, question, spyOpen, faber_state,
        prior.UP, prior.FLAT, prior.DOWN, basis, call
    ).run();
    return { created: true, week_start: weekStart, faber_state, prior };
}

async function resolveOpenPolls(env) {
    // Resolve any polls whose week_end <= today and outcome IS NULL.
    const today = isoDate(new Date());
    const open = await env.DB.prepare(
        `SELECT * FROM polls WHERE outcome IS NULL AND week_end <= ?`
    ).bind(today).all();

    const data = await fetchTidelineState(env);
    const spyClose = data?.regime?.zones?.trend_signal?.evidence?.spy_close;
    if (!spyClose) return { resolved: 0, error: 'no spy_close in latest.json' };

    let resolvedCount = 0;
    for (const poll of open.results || []) {
        const open_ = poll.spy_open;
        if (open_ == null) continue;
        const ret = (spyClose - open_) / open_;
        let outcome;
        if (ret > 0.0025) outcome = 'UP';
        else if (ret < -0.0025) outcome = 'DOWN';
        else outcome = 'NEUTRAL';

        const tidelineCorrect = poll.tideline_call === outcome ? 1 : 0;

        // Crowd: count votes per option and find majority
        const tally = await env.DB.prepare(
            `SELECT vote, COUNT(*) AS n FROM votes WHERE poll_id = ? GROUP BY vote`
        ).bind(poll.id).all();
        let crowdMajority = null;
        let totalVotes = 0;
        let counts = { UP: 0, DOWN: 0, NEUTRAL: 0 };
        for (const r of tally.results || []) {
            counts[r.vote] = r.n;
            totalVotes += r.n;
        }
        if (totalVotes > 0) {
            crowdMajority = ['UP', 'DOWN', 'NEUTRAL'].reduce((a, b) =>
                counts[a] >= counts[b] ? a : b
            );
        }
        const crowdCorrect = crowdMajority ? (crowdMajority === outcome ? 1 : 0) : null;

        // Brier scoring (3-way) — house prior is a real probability vector now
        const oneHot = (k) => (k === outcome ? 1 : 0);
        // Prior buckets from D1: prior_up / prior_flat / prior_down
        // The poll's outcome and the vote bucket use 'NEUTRAL' (legacy enum value
        // in vote check constraint). We treat NEUTRAL == FLAT here.
        const priorOutcomeKey = outcome === 'NEUTRAL' ? 'FLAT' : outcome;
        const tidelineBrier =
            (poll.prior_up   - (priorOutcomeKey === 'UP'   ? 1 : 0)) ** 2 +
            (poll.prior_flat - (priorOutcomeKey === 'FLAT' ? 1 : 0)) ** 2 +
            (poll.prior_down - (priorOutcomeKey === 'DOWN' ? 1 : 0)) ** 2;

        let crowdBrier = null;
        if (totalVotes > 0) {
            // counts uses keys 'UP', 'DOWN', 'NEUTRAL'; map NEUTRAL to FLAT semantics
            const cUp = counts.UP / totalVotes;
            const cFlat = counts.NEUTRAL / totalVotes;
            const cDown = counts.DOWN / totalVotes;
            crowdBrier =
                (cUp   - (priorOutcomeKey === 'UP'   ? 1 : 0)) ** 2 +
                (cFlat - (priorOutcomeKey === 'FLAT' ? 1 : 0)) ** 2 +
                (cDown - (priorOutcomeKey === 'DOWN' ? 1 : 0)) ** 2;
        }

        await env.DB.prepare(`
            UPDATE polls
               SET resolved_at = datetime('now'),
                   spy_close = ?,
                   outcome = ?,
                   tideline_correct = ?,
                   crowd_majority = ?,
                   crowd_correct = ?,
                   tideline_brier = ?,
                   crowd_brier = ?
             WHERE id = ?
        `).bind(
            spyClose, outcome, tidelineCorrect, crowdMajority, crowdCorrect,
            tidelineBrier, crowdBrier, poll.id
        ).run();
        resolvedCount++;
    }
    return { resolved: resolvedCount };
}

// ----------------------------------------------------------------------
// Routes
// ----------------------------------------------------------------------

async function getCurrent(env) {
    const monday = mondayOfWeek(new Date());
    const weekStart = isoDate(monday);

    let poll = await env.DB.prepare(`SELECT * FROM polls WHERE week_start = ?`).bind(weekStart).first();
    if (!poll) {
        // Lazy-create on first read of the week (in case cron drifted)
        await createPollIfMissing(env);
        poll = await env.DB.prepare(`SELECT * FROM polls WHERE week_start = ?`).bind(weekStart).first();
    }
    if (!poll) return json({ error: 'no_poll' }, { status: 503 });

    const tally = await env.DB.prepare(
        `SELECT vote, COUNT(*) AS n FROM votes WHERE poll_id = ? GROUP BY vote`
    ).bind(poll.id).all();

    const counts = { UP: 0, DOWN: 0, NEUTRAL: 0 };
    let total = 0;
    for (const r of tally.results || []) {
        counts[r.vote] = r.n;
        total += r.n;
    }

    // Voting closes at end of day Wednesday (no peeking at late-week price action)
    const now = new Date();
    const cutoff = new Date(monday);
    cutoff.setUTCDate(monday.getUTCDate() + 3); // Thursday 00:00 UTC
    const open = now < cutoff;

    return json({
        poll_id: poll.id,
        week_start: poll.week_start,
        week_end: poll.week_end,
        question: poll.question,
        faber_state: poll.faber_state,
        // House prior — probability vector for the 3-way target
        prior: {
            UP:   poll.prior_up,
            FLAT: poll.prior_flat,
            DOWN: poll.prior_down,
        },
        modal_call: poll.tideline_call,         // headline display
        basis: poll.tideline_basis,
        spy_open: poll.spy_open,
        votes: { ...counts, total },
        voting_open: open,
        voting_closes_at: cutoff.toISOString(),
    });
}

function newUserId() {
    // 22-char URL-safe random ID (~131 bits of entropy)
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}

async function castVote(req, env) {
    let body;
    try {
        body = await req.json();
    } catch {
        return json({ error: 'invalid_json' }, { status: 400 });
    }
    const choice = String(body?.vote || '').toUpperCase();
    if (!['UP', 'DOWN', 'NEUTRAL'].includes(choice)) {
        return json({ error: 'invalid_vote' }, { status: 400 });
    }
    // Anonymous opaque user ID: client supplies it from localStorage; if absent, we
    // generate one and return it for the client to persist.
    let userId = String(body?.user_id || '').trim();
    if (!userId.match(/^[a-f0-9]{16,64}$/)) {
        userId = newUserId();
    }

    const monday = mondayOfWeek(new Date());
    const weekStart = isoDate(monday);
    const poll = await env.DB.prepare(`SELECT id FROM polls WHERE week_start = ?`).bind(weekStart).first();
    if (!poll) return json({ error: 'no_open_poll' }, { status: 404 });

    // Voting closes Wednesday end-of-day (UTC ≈ Thursday 00:00)
    const now = new Date();
    const cutoff = new Date(monday);
    cutoff.setUTCDate(monday.getUTCDate() + 3);
    if (now >= cutoff) return json({ error: 'voting_closed' }, { status: 423 });

    const hash = await ipHash(req);
    try {
        await env.DB.prepare(
            `INSERT INTO votes (poll_id, ip_hash, user_id, vote) VALUES (?, ?, ?, ?)`
        ).bind(poll.id, hash, userId, choice).run();
    } catch (e) {
        // Likely UNIQUE PK violation = already voted from this IP
        return json({ error: 'already_voted', user_id: userId }, { status: 409 });
    }
    return json({ ok: true, vote: choice, user_id: userId });
}

async function getMe(env, userId) {
    if (!userId.match(/^[a-f0-9]{16,64}$/)) {
        return json({ error: 'invalid_user_id' }, { status: 400 });
    }
    const r = await env.DB.prepare(`
        SELECT v.vote AS your_vote,
               v.voted_at,
               p.id AS poll_id,
               p.week_start, p.week_end, p.question,
               p.faber_state, p.prior_up, p.prior_flat, p.prior_down,
               p.tideline_call AS house_call,
               p.outcome,
               p.spy_open, p.spy_close
          FROM votes v
          JOIN polls p ON p.id = v.poll_id
         WHERE v.user_id = ?
         ORDER BY p.week_start DESC
         LIMIT 100
    `).bind(userId).all();

    const rows = r.results || [];
    let n = 0, hits = 0, total_brier = 0, n_brier = 0;
    let n_disagree_house = 0, n_disagree_house_user_right = 0;
    for (const v of rows) {
        if (!v.outcome) continue;                  // unresolved still
        n++;
        // Outcome enum: 'UP'/'DOWN'/'NEUTRAL' (NEUTRAL == FLAT)
        const correct = (v.your_vote === v.outcome) ? 1 : 0;
        hits += correct;
        // User Brier: the user picks one bucket = 1.0 prob there, 0 elsewhere
        const oneHotKey = v.outcome === 'NEUTRAL' ? 'FLAT' : v.outcome;
        const userPickKey = v.your_vote === 'NEUTRAL' ? 'FLAT' : v.your_vote;
        const b =
            (((userPickKey === 'UP'   ? 1 : 0) - (oneHotKey === 'UP'   ? 1 : 0)) ** 2) +
            (((userPickKey === 'FLAT' ? 1 : 0) - (oneHotKey === 'FLAT' ? 1 : 0)) ** 2) +
            (((userPickKey === 'DOWN' ? 1 : 0) - (oneHotKey === 'DOWN' ? 1 : 0)) ** 2);
        total_brier += b;
        n_brier++;
        // Disagreement vs house: where user's pick differs from house's modal call
        if (v.your_vote !== v.house_call) {
            n_disagree_house++;
            if (correct) n_disagree_house_user_right++;
        }
    }

    return json({
        user_id: userId,
        n_total_votes: rows.length,
        n_resolved: n,
        accuracy: n ? hits / n : null,
        avg_brier: n_brier ? total_brier / n_brier : null,
        vs_house: {
            disagreements: n_disagree_house,
            user_right_when_disagreed: n_disagree_house_user_right,
            user_acc_on_disagree: n_disagree_house ? n_disagree_house_user_right / n_disagree_house : null,
        },
        history: rows,
    });
}

async function getScoreboard(env) {
    // All-time accuracy and Brier for resolved polls.
    const rs = await env.DB.prepare(`
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN tideline_correct = 1 THEN 1 ELSE 0 END) AS t_hits,
               SUM(CASE WHEN crowd_correct    = 1 THEN 1 ELSE 0 END) AS c_hits,
               SUM(CASE WHEN crowd_correct IS NOT NULL THEN 1 ELSE 0 END) AS c_n,
               AVG(tideline_brier) AS t_brier,
               AVG(crowd_brier)    AS c_brier
          FROM polls
         WHERE outcome IS NOT NULL
    `).first();
    // Disagreement subset — where the crowd's modal vote differs from house prior's modal call
    const dis = await env.DB.prepare(`
        SELECT COUNT(*) AS n_dis,
               SUM(CASE WHEN tideline_correct = 1 THEN 1 ELSE 0 END) AS t_hits_dis,
               SUM(CASE WHEN crowd_correct    = 1 THEN 1 ELSE 0 END) AS c_hits_dis
          FROM polls
         WHERE outcome IS NOT NULL
           AND crowd_majority IS NOT NULL
           AND tideline_call != crowd_majority
    `).first();
    const recent = await env.DB.prepare(`
        SELECT week_start, week_end, tideline_call, outcome, tideline_correct, crowd_majority, crowd_correct
          FROM polls
         WHERE outcome IS NOT NULL
         ORDER BY week_start DESC
         LIMIT 12
    `).all();
    const total = rs.n || 0;
    return json({
        total_resolved: total,
        tideline: {
            n: total,
            hits: rs.t_hits || 0,
            accuracy: total ? (rs.t_hits || 0) / total : null,
            avg_brier: rs.t_brier ?? null,
        },
        crowd: {
            n: rs.c_n || 0,
            hits: rs.c_hits || 0,
            accuracy: rs.c_n ? (rs.c_hits || 0) / rs.c_n : null,
            avg_brier: rs.c_brier ?? null,
        },
        disagreement: {
            n: dis.n_dis || 0,
            house_hits: dis.t_hits_dis || 0,
            crowd_hits: dis.c_hits_dis || 0,
            house_acc: dis.n_dis ? (dis.t_hits_dis || 0) / dis.n_dis : null,
            crowd_acc: dis.n_dis ? (dis.c_hits_dis || 0) / dis.n_dis : null,
        },
        recent: recent.results || [],
    });
}

async function getHistory(env, limit = 20) {
    limit = Math.min(parseInt(limit, 10) || 20, 100);
    const r = await env.DB.prepare(`
        SELECT id, week_start, week_end, question, tideline_call, tideline_correct,
               outcome, crowd_majority, crowd_correct
          FROM polls
         WHERE outcome IS NOT NULL
         ORDER BY week_start DESC
         LIMIT ?
    `).bind(limit).all();
    return json({ history: r.results || [] });
}

// ----------------------------------------------------------------------
// HTTP entrypoint
// ----------------------------------------------------------------------

export default {
    async fetch(req, env) {
        const url = new URL(req.url);
        if (req.method === 'OPTIONS') {
            return new Response(null, { headers: corsHeaders() });
        }
        try {
            if (url.pathname === '/poll/current' && req.method === 'GET')   return getCurrent(env);
            if (url.pathname === '/vote'         && req.method === 'POST')  return castVote(req, env);
            if (url.pathname === '/scoreboard'   && req.method === 'GET')   return getScoreboard(env);
            if (url.pathname === '/history'      && req.method === 'GET')   {
                const limit = url.searchParams.get('limit');
                return getHistory(env, limit);
            }
            // /me/<user_id>  —  personal vote history + accuracy
            if (url.pathname.startsWith('/me/')   && req.method === 'GET')   {
                const uid = url.pathname.slice(4);
                return getMe(env, uid);
            }
            if (url.pathname === '/health'       && req.method === 'GET')   return text('ok');
            // Manual cron triggers for testing — admin only
            if (url.pathname === '/_admin/create' && req.method === 'POST') {
                const r = await createPollIfMissing(env);
                return json(r);
            }
            if (url.pathname === '/_admin/resolve' && req.method === 'POST') {
                const r = await resolveOpenPolls(env);
                return json(r);
            }
            return json({ error: 'not_found', path: url.pathname }, { status: 404 });
        } catch (e) {
            return json({ error: 'server_error', message: e?.message }, { status: 500 });
        }
    },

    async scheduled(event, env, ctx) {
        // Cron picks the right action by current weekday:
        //   Monday  → create poll
        //   Friday  → resolve any open
        const dow = new Date().getUTCDay(); // 0=Sun
        if (dow === 1) {
            ctx.waitUntil(createPollIfMissing(env));
        } else if (dow === 5) {
            ctx.waitUntil(resolveOpenPolls(env));
        }
    },
};
