-- Tideline voting D1 schema
-- One poll per Mon-Fri trading week. Tideline + crowd both make a directional
-- forecast on SPY's Friday close vs Monday close. Friday after market close,
-- the resolution job computes outcome + scores both sides.

CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL UNIQUE,         -- e.g. "2026-04-27" (Monday)
    week_end TEXT NOT NULL,                  -- "2026-05-01" (Friday)
    question TEXT NOT NULL,                  -- "Will SPY close higher this Friday than Monday's close?"
    spy_open REAL,                           -- SPY close on week_start (the reference)
    tideline_call TEXT NOT NULL,             -- 'UP' | 'DOWN' | 'NEUTRAL'
    tideline_basis TEXT,                     -- e.g. "Faber GREEN — both 50DMA and 200DMA bullish"
    tideline_confidence REAL NOT NULL,       -- 0.0–1.0; for Brier scoring
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- resolution fields (filled Friday after close)
    resolved_at TEXT,
    spy_close REAL,
    outcome TEXT,                            -- 'UP' | 'DOWN' | 'NEUTRAL'
    tideline_correct INTEGER,                -- 1 / 0 (NULL until resolved)
    crowd_majority TEXT,                     -- which option got most votes
    crowd_correct INTEGER,                   -- 1 / 0
    crowd_brier REAL,                        -- crowd Brier score (0 = perfect)
    tideline_brier REAL                      -- Tideline Brier score
);

CREATE INDEX IF NOT EXISTS polls_week_start_idx ON polls(week_start);
CREATE INDEX IF NOT EXISTS polls_resolved_idx ON polls(resolved_at);

CREATE TABLE IF NOT EXISTS votes (
    poll_id INTEGER NOT NULL,
    ip_hash TEXT NOT NULL,                   -- sha256 of (IP + daily salt)
    vote TEXT NOT NULL CHECK (vote IN ('UP', 'DOWN', 'NEUTRAL')),
    voted_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (poll_id, ip_hash),
    FOREIGN KEY (poll_id) REFERENCES polls(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS votes_poll_idx ON votes(poll_id);
