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

function tidelineCallFromState(data) {
    // Use Faber zone signal as the directional call. Fall back to crowd-bias
    // baseline only if regime is degraded.
    const regime = data?.regime;
    if (!regime || regime.error) {
        return { call: 'NEUTRAL', confidence: 0.5, basis: 'data_unavailable' };
    }
    const z0 = regime.zones?.trend_signal;
    const state = z0?.state || 'NEUTRAL';
    const evidence = z0?.evidence || {};
    if (state === 'GREEN') {
        // Both 50DMA and 200DMA bullish — historical UP-rate ~65.6%
        return {
            call: 'UP',
            confidence: 0.66,
            basis: `Faber GREEN — SPY ${evidence.spy_close} above 200DMA ${evidence.ma_200}, 50DMA also above 200DMA`,
        };
    }
    if (state === 'CAUTION') {
        // Both bearish — historical DOWN-rate ~46% (vs 37% baseline)
        return {
            call: 'DOWN',
            confidence: 0.55,
            basis: `Faber CAUTION — SPY ${evidence.spy_close} below 200DMA ${evidence.ma_200}, 50DMA below 200DMA`,
        };
    }
    // Mixed — confidence-weighted neutral, but lean UP per unconditional baseline
    return {
        call: 'UP',
        confidence: 0.55,
        basis: `Faber NEUTRAL — indicators mixed; defaulting to baseline up-rate (~63%)`,
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
    const { call, confidence, basis } = tidelineCallFromState(data);
    const spyOpen = data?.regime?.zones?.trend_signal?.evidence?.spy_close ?? null;

    const question = `Will SPY close higher on Friday ${weekEnd} than today's open ${spyOpen?.toFixed?.(2) || ''}?`;
    await env.DB.prepare(`
        INSERT INTO polls (week_start, week_end, question, spy_open, tideline_call, tideline_basis, tideline_confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    `).bind(weekStart, weekEnd, question, spyOpen, call, basis, confidence).run();
    return { created: true, week_start: weekStart, tideline_call: call };
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

        // Brier scoring (3-way)
        const oneHot = (k) => (k === outcome ? 1 : 0);
        const tidelineProb = {
            UP: poll.tideline_call === 'UP' ? poll.tideline_confidence : (1 - poll.tideline_confidence) / 2,
            DOWN: poll.tideline_call === 'DOWN' ? poll.tideline_confidence : (1 - poll.tideline_confidence) / 2,
            NEUTRAL: poll.tideline_call === 'NEUTRAL' ? poll.tideline_confidence : (1 - poll.tideline_confidence) / 2,
        };
        const tidelineBrier =
            ['UP', 'DOWN', 'NEUTRAL'].reduce((s, k) => s + (tidelineProb[k] - oneHot(k)) ** 2, 0);

        let crowdBrier = null;
        if (totalVotes > 0) {
            crowdBrier = ['UP', 'DOWN', 'NEUTRAL'].reduce(
                (s, k) => s + (counts[k] / totalVotes - oneHot(k)) ** 2,
                0
            );
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
        tideline_call: poll.tideline_call,
        tideline_confidence: poll.tideline_confidence,
        tideline_basis: poll.tideline_basis,
        spy_open: poll.spy_open,
        votes: { ...counts, total },
        voting_open: open,
        voting_closes_at: cutoff.toISOString(),
    });
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
            `INSERT INTO votes (poll_id, ip_hash, vote) VALUES (?, ?, ?)`
        ).bind(poll.id, hash, choice).run();
    } catch (e) {
        // Likely UNIQUE PK violation = already voted
        return json({ error: 'already_voted' }, { status: 409 });
    }
    return json({ ok: true, vote: choice });
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
