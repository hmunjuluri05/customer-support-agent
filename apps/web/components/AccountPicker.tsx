'use client';

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
export function AccountPicker({
  accounts,
  busy,
  error,
  onPick,
  onRetry,
}: {
  accounts: DemoAccount[];
  busy: boolean;
  error: string | null;
  onPick: (slug: string) => void;
  onRetry: () => void;
}) {
  return (
    <div className="picker">
      <div className="picker-inner">
        <h2>Sign in as a demo customer</h2>
        <p className="lede">
          Three synthetic accounts, chosen so the same request — a $15.00 refund on a $40.00
          invoice — produces a different, correctly-cited outcome on each.
        </p>

        {error !== null && (
          <div className="error-box" style={{ marginBottom: 16 }}>
            <strong>Could not load accounts</strong>
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
