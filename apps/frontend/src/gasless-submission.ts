import {
  authorizeGaslessClaim,
  getGaslessNetwork,
  getGaslessSubmission,
  prepareGaslessClaim,
  type ClaimPayload,
  type ClaimReceipt,
  type GaslessSubmission,
} from './api.ts'
import {
  browserWallet,
  connectWallet,
  signForwardRequest,
  switchWalletChain,
  type EthereumProvider,
} from './wallet.ts'

export type SubmissionProgress =
  | 'Connecting wallet'
  | 'Switching network'
  | 'Preparing claim'
  | 'Awaiting wallet signature'
  | 'Queued for sponsorship'
  | 'Broadcasting transaction'
  | 'Waiting for confirmations'

export class GaslessSubmissionTerminalError extends Error {
  readonly submissionId: string
  readonly state: 'failed' | 'expired'
  readonly errorCode: string | null

  constructor(
    submissionId: string,
    state: 'failed' | 'expired',
    errorCode: string | null,
  ) {
    // Preserve machine-readable state for the form's idempotency decision while
    // giving the user a concise, support-friendly submission identifier.
    super(
      `Sponsored transaction ${submissionId} ended in ${state}` +
        (errorCode ? ` (${errorCode})` : ''),
    )
    this.name = 'GaslessSubmissionTerminalError'
    this.submissionId = submissionId
    this.state = state
    this.errorCode = errorCode
  }
}

type SubmitGaslessClaimOptions = {
  claim: ClaimPayload
  insurerApiKey: string
  idempotencyKey: string
  signal?: AbortSignal
  onProgress?: (progress: SubmissionProgress) => void
  provider?: EthereumProvider
}

function assertPreparedRequest(
  submission: GaslessSubmission,
  signer: string,
): asserts submission is GaslessSubmission & {
  typed_data: NonNullable<GaslessSubmission['typed_data']>
} {
  // Treat every API response as untrusted at the wallet boundary. These checks
  // bind the signature to the connected account and to the deployment fetched
  // before preparation, catching proxy/configuration drift before prompting.
  const typedData = submission.typed_data
  if (submission.state !== 'prepared' || typedData === null) {
    throw new Error(
      `Submission ${submission.submission_id} is not available for wallet authorization.`,
    )
  }
  if (
    submission.signer_address.toLowerCase() !== signer.toLowerCase() ||
    typedData.message.from.toLowerCase() !== signer.toLowerCase()
  ) {
    throw new Error('The connected wallet does not match the insurer credential.')
  }
  if (
    typedData.domain.chainId !== submission.chain_id ||
    typedData.domain.verifyingContract.toLowerCase() !==
      submission.forwarder_address.toLowerCase() ||
    typedData.message.to.toLowerCase() !== submission.contract_address.toLowerCase()
  ) {
    throw new Error('The prepared signature domain does not match the deployment.')
  }
}

function wait(milliseconds: number, signal?: AbortSignal): Promise<void> {
  // Make polling delay cancellable so navigation/unmount stops both the timer
  // and the next network request instead of updating an abandoned component.
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const onAbort = () => {
      window.clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    const timer = window.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, milliseconds)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

function progressFor(submission: GaslessSubmission): SubmissionProgress {
  // Collapse durable backend states into language meaningful to an insurer;
  // detailed state and error codes remain available in API responses and logs.
  if (submission.state === 'authorized') return 'Queued for sponsorship'
  if (submission.state === 'signed') return 'Broadcasting transaction'
  return 'Waiting for confirmations'
}

async function pollUntilConfirmed(
  initial: GaslessSubmission,
  insurerApiKey: string,
  signal: AbortSignal | undefined,
  onProgress: ((progress: SubmissionProgress) => void) | undefined,
): Promise<ClaimReceipt> {
  // Poll the database-backed status resource rather than Ethereum. The relayer
  // owns nonce allocation and receipts; browser retries therefore cannot create
  // duplicate chain writes. A few transient read failures are tolerated because
  // the submission continues durably after the browser loses connectivity.
  let submission = initial
  let transientFailures = 0
  while (true) {
    if (submission.state === 'confirmed' && submission.receipt) {
      return submission.receipt
    }
    if (submission.state === 'failed' || submission.state === 'expired') {
      throw new GaslessSubmissionTerminalError(
        submission.submission_id,
        submission.state,
        submission.error_code,
      )
    }
    onProgress?.(progressFor(submission))
    await wait(Math.min(10_000, Math.max(500, submission.poll_after_ms)), signal)
    try {
      submission = await getGaslessSubmission(
        submission.submission_id,
        insurerApiKey,
        signal,
      )
      transientFailures = 0
    } catch (error) {
      transientFailures += 1
      if (transientFailures >= 3) {
        throw new Error(
          `Submission ${submission.submission_id} remains durable, but its status ` +
            `could not be checked: ${error instanceof Error ? error.message : 'network error'}`,
        )
      }
    }
  }
}

export async function submitGaslessClaim({
  claim,
  insurerApiKey,
  idempotencyKey,
  signal,
  onProgress,
  provider = browserWallet(),
}: SubmitGaslessClaimOptions): Promise<ClaimReceipt> {
  // Orchestrate discovery -> preparation -> wallet authorization -> polling.
  // The browser never receives the relayer key, and FastAPI never receives the
  // insurer wallet key. Each side sees only the data required for its role.
  onProgress?.('Connecting wallet')
  const network = await getGaslessNetwork(signal)
  const signer = await connectWallet(provider)

  onProgress?.('Switching network')
  await switchWalletChain(network.chain_id, provider)

  onProgress?.('Preparing claim')
  let prepared = await prepareGaslessClaim(
    claim,
    insurerApiKey,
    signer,
    idempotencyKey,
    signal,
  )
  if (
    prepared.chain_id !== network.chain_id ||
    prepared.contract_address.toLowerCase() !== network.contract_address.toLowerCase() ||
    prepared.forwarder_address.toLowerCase() !== network.forwarder_address.toLowerCase()
  ) {
    throw new Error('The active gasless deployment changed during preparation.')
  }
  while (prepared.state === 'preparing') {
    // A matching Idempotency-Key can observe an API replica that is still doing
    // the original IPFS round-trip. Wait for that durable lease to resolve rather
    // than starting a second upload or allocating a second forwarder nonce.
    await wait(Math.min(10_000, Math.max(500, prepared.poll_after_ms)), signal)
    prepared = await getGaslessSubmission(
      prepared.submission_id,
      insurerApiKey,
      signal,
    )
  }
  if (prepared.state !== 'prepared') {
    return pollUntilConfirmed(prepared, insurerApiKey, signal, onProgress)
  }
  assertPreparedRequest(prepared, signer)

  onProgress?.('Awaiting wallet signature')
  const signature = await signForwardRequest(signer, prepared.typed_data, provider)

  let authorized: GaslessSubmission
  try {
    authorized = await authorizeGaslessClaim(
      prepared.submission_id,
      signature,
      insurerApiKey,
      signal,
    )
  } catch (authorizationError) {
    // The POST may have reached FastAPI before the connection failed. Read the
    // durable state before asking the insurer to sign or submit anything again.
    const recovered = await getGaslessSubmission(
      prepared.submission_id,
      insurerApiKey,
      signal,
    )
    if (recovered.state === 'prepared') throw authorizationError
    authorized = recovered
  }
  return pollUntilConfirmed(authorized, insurerApiKey, signal, onProgress)
}
