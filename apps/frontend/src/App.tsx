// Page composition stays here; request coordination and domain-specific UI live
// in the workspace hook and focused components below this boundary.
import { ClaimForm } from './components/ClaimForm.tsx'
import { ClaimsDashboard } from './components/ClaimsDashboard.tsx'
import { ReceiptCard } from './components/ReceiptCard.tsx'
import { useClaimsWorkspace } from './hooks/useClaimsWorkspace.ts'

// Keep these public exports for component-level tests and downstream consumers.
export { ClaimsDashboard } from './components/ClaimsDashboard.tsx'
export { ReceiptCard } from './components/ReceiptCard.tsx'

function DataBoundaryAside() {
  return (
    <aside className="space-y-5 lg:sticky lg:top-6">
      <section className="rounded-3xl bg-ink p-6 text-white">
        <p className="text-xs font-bold tracking-[0.16em] text-coral uppercase">
          Data boundary
        </p>
        <h2 className="mt-2 text-xl font-bold">What goes where?</h2>
        <dl className="mt-5 space-y-5 text-sm">
          <div>
            <dt className="font-bold text-white">On Sepolia</dt>
            <dd className="mt-1 leading-6 text-white/60">
              Claim ID, Keccak-256 hash, IPFS pointer and status.
            </dd>
          </div>
          <div className="border-t border-white/10 pt-5">
            <dt className="font-bold text-white">On public IPFS</dt>
            <dd className="mt-1 leading-6 text-white/60">
              The synthetic JSON payload. No real personal data.
            </dd>
          </div>
          <div className="border-t border-white/10 pt-5">
            <dt className="font-bold text-white">In the browser</dt>
            <dd className="mt-1 leading-6 text-white/60">
              Form state and the public transaction receipt only.
            </dd>
          </div>
        </dl>
      </section>

      <section className="rounded-3xl border border-ink/8 bg-white p-6">
        <p className="text-xs font-bold tracking-[0.16em] text-teal uppercase">
          Network
        </p>
        <div className="mt-3 flex items-center justify-between">
          <span className="font-bold text-ink">Ethereum Sepolia</span>
          <span className="rounded-full bg-mint px-2.5 py-1 text-xs font-bold text-teal">
            Testnet
          </span>
        </div>
        <p className="mt-3 text-sm leading-6 text-slate">
          Submissions spend test ETH and are publicly visible.
        </p>
      </section>
    </aside>
  )
}

/**
 * Application shell only: domain workflows live in focused components and the
 * claims workspace hook so this page remains easy to scan and change safely.
 */
function App() {
  const workspace = useClaimsWorkspace()

  return (
    <div className="min-h-screen">
      <header className="border-b border-white/10 bg-ink text-white">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-5 py-4 sm:px-8 lg:px-12">
          <a
            href="#main"
            className="flex items-center gap-3 rounded-md focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-coral"
          >
            <span className="grid size-10 place-items-center rounded-xl bg-coral font-black text-ink">
              CR
            </span>
            <span>
              <span className="block text-sm font-bold">Claims Registry</span>
              <span className="block text-xs text-white/55">
                Sepolia prototype
              </span>
            </span>
          </a>
          <div className="flex items-center gap-3">
            <a
              href="#claims"
              className="hidden rounded-full px-3 py-2 text-xs font-semibold text-white/70 transition hover:bg-white/5 hover:text-white sm:inline-flex"
            >
              View all claims
            </a>
            <div className="flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-3 py-2 text-xs font-semibold text-white/75">
              <span className="size-2 rounded-full bg-emerald-400 shadow-[0_0_0_4px_rgba(52,211,153,0.12)]" />
              Research test data only
            </div>
          </div>
        </div>
      </header>

      <main
        id="main"
        className="mx-auto max-w-7xl px-5 py-10 sm:px-8 lg:px-12 lg:py-16"
      >
        <section className="grid items-end gap-8 lg:grid-cols-[1.15fr_0.85fr]">
          <div>
            <p className="mb-5 inline-flex rounded-full border border-teal/15 bg-mint px-3 py-1.5 text-xs font-bold tracking-[0.16em] text-teal uppercase">
              Dissertation milestone M1
            </p>
            <h1 className="max-w-3xl text-4xl leading-[1.05] font-black tracking-[-0.035em] text-ink sm:text-6xl">
              Submit once. Verify everywhere.
            </h1>
            <p className="mt-5 max-w-2xl text-base leading-7 text-slate sm:text-lg">
              This form sends a synthetic claim to FastAPI. The backend pins the
              canonical JSON to IPFS, checks the bytes, then anchors its hash and
              CID on Ethereum Sepolia.
            </p>
          </div>

          <ol className="grid grid-cols-2 gap-2 rounded-2xl border border-ink/8 bg-white p-2 shadow-sm sm:grid-cols-4">
            {[
              ['01', 'Validate'],
              ['02', 'Pin to IPFS'],
              ['03', 'Anchor'],
              ['04', 'Match & score'],
            ].map(([number, label]) => (
              <li key={number} className="rounded-xl bg-sand px-3 py-4 text-center">
                <span className="block text-xs font-black tracking-[0.18em] text-coral-dark">
                  {number}
                </span>
                <span className="mt-1 block text-xs font-semibold text-ink sm:text-sm">
                  {label}
                </span>
              </li>
            ))}
          </ol>
        </section>

        <div className="mt-10 grid items-start gap-8 lg:grid-cols-[minmax(0,1fr)_22rem]">
          <ClaimForm onSubmitted={workspace.acceptSubmittedReceipt} />
          <DataBoundaryAside />
        </div>

        {workspace.receipt && (
          <div id="claim-details" className="mt-8 scroll-mt-6">
            {workspace.detailsError && (
              <div
                role="alert"
                className="mb-3 rounded-2xl border border-red-200 bg-red-50 px-5 py-3 text-sm font-medium text-red-800"
              >
                Sepolia details are shown below, but the stored model explanation
                could not be loaded: {workspace.detailsError}
              </div>
            )}
            <ReceiptCard
              receipt={workspace.receipt}
              assessmentPollingError={workspace.assessmentPollingError}
              onCheckAssessment={workspace.checkPendingAssessment}
            />
          </div>
        )}

        <ClaimsDashboard
          claims={workspace.claims}
          page={workspace.page}
          pageSize={workspace.pageSize}
          totalItems={workspace.totalItems}
          totalPages={workspace.totalPages}
          isLoading={workspace.isLoading}
          error={workspace.error}
          selectedClaimId={workspace.receipt?.claim_id ?? null}
          openingClaimId={workspace.openingClaimId}
          onRefresh={() => void workspace.refresh()}
          onClaimSelect={(claim) => void workspace.showClaimDetails(claim)}
          onPageChange={workspace.setPage}
          onPageSizeChange={workspace.changePageSize}
        />
      </main>

      <footer className="border-t border-ink/8 bg-white/60">
        <div className="mx-auto flex max-w-7xl flex-col gap-2 px-5 py-6 text-xs text-slate sm:flex-row sm:items-center sm:justify-between sm:px-8 lg:px-12">
          <span>Decentralized Claims Registry · Research prototype</span>
          <span>React → FastAPI → Sepolia → Kafka → XGBoost</span>
        </div>
      </footer>
    </div>
  )
}

export default App
