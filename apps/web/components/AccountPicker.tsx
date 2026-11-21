'use client';

import type { ResumableSession } from '@/lib/client';
import type { DemoAccount } from '@/lib/events';
import { money } from '@/lib/format';

/**
 * Account selection, which is also the identity step.
 *
 * Choosing here is the only way an account is bound to a session: the slug is
 * sent to POST /api/sessions and the server records the binding. Nothing said
 * in a chat message can change it, which is what makes the cross-account
 * refund probe fail server-side instead of relying on the model to refuse.
 */
/** Coarse on purpose: a session lives four hours, so minutes are enough. */
function ago(iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return `${hours} hour${hours === 1 ? '' : 's'} ago`;
}

export function AccountPicker({
  accounts,
  busy,
  error,
  resumable,
  onPick,
  onResume,
  onRetry,
}: {
  accounts: DemoAccount[];
  busy: boolean;
  error: string | null;
  /** Sessions this browser can resume, newest first, one per account. */
  resumable: ResumableSession[];
  onPick: (slug: string) => void;
  onResume: (session: ResumableSession) => void;
  onRetry: () => void;
}) {
  return (
    <div className="picker">
      <div className="picker-inner">
        <h2>
          {resumable.length === 0 ? 'Sign in as a demo customer' : 'Resume, or start fresh'}
        </h2>
        <p className="lede">
          Three synthetic accounts, chosen so the same request — a $15.00 refund on a $40.00
          invoice — produces a different, correctly-cited outcome on each.
        </p>

        {/* Offered, never automatic. Closing a browser is a reasonable way to
            mean "I am done", and a shared machine must not drop the next
            person into the previous one's transcript. */}
        {resumable.map((session) => (
          <div className="resume-card" key={session.session_id}>
            <div>
              <strong>
                Resume · {session.account?.customer_name ?? session.account?.label ?? 'No account'}
              </strong>
              <div className="invoice-note">
                {session.messages} message{session.messages === 1 ? '' : 's'}
                {session.last_activity !== null && ` · last active ${ago(session.last_activity)}`}
              </div>
            </div>
            <button
              type="button"
              className="btn primary"
              disabled={busy}
              onClick={() => onResume(session)}
            >
              Resume
            </button>
          </div>
        ))}

        {error !== null && (
          <div className="error-box" style={{ marginBottom: 16 }}>
            <strong>Something went wrong</strong>
            <div>{error}</div>
            <button type="button" className="btn small" style={{ marginTop: 8 }} onClick={onRetry}>
              Try again
            </button>
          </div>
        )}

        <div className="account-grid">
          {accounts.map((account) => {
            const refundable = account.invoices.reduce(
              (total, invoice) => total + invoice.amount_minor - invoice.refunded_minor,
              0,
            );
            return (
              <button
                type="button"
                className="account-card"
                key={account.slug}
                disabled={busy}
                onClick={() => onPick(account.slug)}
              >
                <span className="name">{account.customer_name}</span>
                <span className="label">{account.label}</span>
                <span className="chips">
                  <span className="chip">{account.tier}</span>
                  <span
                    className={`chip ${account.contract_status === 'active' ? 'good' : 'warn'}`}
                  >
                    {account.contract_status}
                  </span>
                </span>
                <span className="invoice-note">
                  {account.invoices.length} invoice{account.invoices.length === 1 ? '' : 's'} ·{' '}
                  {money(refundable)} refundable
                </span>
              </button>
            );
          })}
        </div>

        {accounts.length === 0 && error === null && (
          <div className="center-note">Loading demo accounts…</div>
        )}
      </div>
    </div>
  );
}
