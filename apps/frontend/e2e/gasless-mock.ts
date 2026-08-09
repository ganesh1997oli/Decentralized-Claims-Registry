import type { Page } from '@playwright/test'

export const GASLESS_SIGNER = '0x2222222222222222222222222222222222222222'
export const GASLESS_SUBMISSION_ID = '11111111-1111-4111-8111-111111111111'
export const GASLESS_CONTRACT = '0x3333333333333333333333333333333333333333'
export const GASLESS_FORWARDER = '0x4444444444444444444444444444444444444444'

const signature = `0x${'ab'.repeat(65)}`

export async function installMockWallet(page: Page): Promise<void> {
  await page.addInitScript(
    ({ address, walletSignature }) => {
      ;(globalThis as { ethereum?: unknown }).ethereum = {
        request: async ({ method }: { method: string }) => {
          if (method === 'eth_requestAccounts') return [address]
          if (method === 'wallet_switchEthereumChain') return null
          if (method === 'eth_signTypedData_v4') return walletSignature
          throw new Error(`Unexpected wallet method ${method}`)
        },
      }
    },
    { address: GASLESS_SIGNER, walletSignature: signature },
  )
}

export function gaslessNetwork() {
  return {
    chain_id: 11155111,
    contract_address: GASLESS_CONTRACT,
    forwarder_address: GASLESS_FORWARDER,
    domain_name: 'ClaimsRegistryForwarder',
    domain_version: '1',
  }
}

export function preparedGaslessSubmission() {
  return {
    submission_id: GASLESS_SUBMISSION_ID,
    state: 'prepared',
    signer_address: GASLESS_SIGNER,
    chain_id: 11155111,
    contract_address: GASLESS_CONTRACT,
    forwarder_address: GASLESS_FORWARDER,
    claim_hash: `0x${'12'.repeat(32)}`,
    data_pointer: 'ipfs://claim',
    deadline: 2_000_000_000,
    typed_data: {
      types: {
        EIP712Domain: [
          { name: 'name', type: 'string' },
          { name: 'version', type: 'string' },
          { name: 'chainId', type: 'uint256' },
          { name: 'verifyingContract', type: 'address' },
        ],
        ForwardRequest: [
          { name: 'from', type: 'address' },
          { name: 'to', type: 'address' },
          { name: 'value', type: 'uint256' },
          { name: 'gas', type: 'uint256' },
          { name: 'nonce', type: 'uint256' },
          { name: 'deadline', type: 'uint48' },
          { name: 'data', type: 'bytes' },
        ],
      },
      primaryType: 'ForwardRequest',
      domain: {
        name: 'ClaimsRegistryForwarder',
        version: '1',
        chainId: 11155111,
        verifyingContract: GASLESS_FORWARDER,
      },
      message: {
        from: GASLESS_SIGNER,
        to: GASLESS_CONTRACT,
        value: '0',
        gas: '250000',
        nonce: '7',
        deadline: '2000000000',
        data: '0x1234',
      },
    },
    receipt: null,
    error_code: null,
    poll_after_ms: 250,
  }
}

export function authorizedGaslessSubmission() {
  return {
    ...preparedGaslessSubmission(),
    state: 'authorized',
    typed_data: null,
  }
}

export function confirmedGaslessSubmission(receipt: Record<string, unknown>) {
  return {
    ...authorizedGaslessSubmission(),
    state: 'confirmed',
    receipt,
  }
}
