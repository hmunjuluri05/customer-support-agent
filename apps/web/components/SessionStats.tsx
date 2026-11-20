import type { LatencyStat, SessionMetrics } from '@/lib/client';
import { duration, usd } from '@/lib/format';

/**
 * Session rollup.
 *
 * Read from the server's metric rows, so it survives a refresh and counts the
 * turns the database recorded rather than the ones this tab watched.
 *
 * Percentiles carry their sample count because that is what makes them
 * readable: "p95 1.9 s (4)" is honestly the slowest of four turns, while a
 * bare "p95" implies a distribution a demo session does not have.
 */
export function SessionStats({ metrics }: { metrics: SessionMetrics | null }) {
  if (metrics === null || metrics.turns === 0) {
    return (
      <div className="rail-card">
        <h2>Session metrics</h2>
        <div className="hint">Numbers appear once a turn has finished.</div>
      </div>
    );
  }

  const chat = metrics.latency.chat;
  const voice = metrics.latency.voice;
  const grounded = metrics.grounding.grounded;
  const weak = metrics.grounding.weak;
  const unsupported = metrics.grounding.unsupported;

  return (
    <div className="rail-card">
      <h2>Session metrics</h2>
      <dl className="kv">
        <dt>Turns</dt>
        <dd>
          {metrics.turns}
          {metrics.turns_by_mode.voice > 0 && (
            <span className="hint-inline"> · {metrics.turns_by_mode.voice} spoken</span>
          )}
        </dd>
        <dt>Tool calls</dt>
        <dd>{metrics.tool_calls}</dd>
        <dt>Tokens</dt>
        <dd>
          {metrics.tokens_in} in / {metrics.tokens_out} out
        </dd>
        <dt>Cost</dt>
        <dd>
          {metrics.cost_estimate_usd === null ? '--' : `~${usd(metrics.cost_estimate_usd)}`}
          {metrics.turns_priced < metrics.turns && (
            <span className="hint-inline"> · {metrics.turns_priced} of {metrics.turns} priced</span>
          )}
        </dd>
      </dl>

      <Latency label="Typed turns" stat={chat.total} ttft={chat.ttft} />
      {voice.total.samples > 0 && <Latency label="Spoken turns" stat={voice.total} ttft={voice.ttft} />}

      {grounded + weak + unsupported > 0 && (
        <div className="chips" style={{ marginTop: 10 }}>
          {grounded > 0 && <span className="chip good">{grounded} grounded</span>}
          {weak > 0 && <span className="chip warn">{weak} weak</span>}
          {unsupported > 0 && <span className="chip bad">{unsupported} unsupported</span>}
        </div>
      )}

      {metrics.cost_estimate_usd !== null && (
        <div className="hint">
          Cost is estimated from configured rates effective{' '}
          {metrics.cost_rates_effective ?? 'unknown'}, not billed usage.
        </div>
      )}
    </div>
  );
}

function Latency({
  label,
  stat,
  ttft,
}: {
  label: string;
  stat: LatencyStat;
  ttft: LatencyStat;
}) {
  if (stat.samples === 0) return null;
  return (
    <div className="latency">
      <div className="section-label">{label}</div>
      <div className="metrics-line">
        <span>
          p50 {duration(stat.p50_ms)} <span className="dim">({stat.samples})</span>
        </span>
        <span>p95 {duration(stat.p95_ms)}</span>
        {ttft.samples > 0 && <span>ttft p50 {duration(ttft.p50_ms)}</span>}
      </div>
    </div>
  );
}
