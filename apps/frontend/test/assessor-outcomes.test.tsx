/**
 * App-level tests for the isolated human-assessor workflow.
 *
 * These tests live outside production `src`, matching the sibling contracts
 * application's `test` directory. They cover both the locked review surface and
 * its HTTP boundary without mixing assessor cases into the public claim
 * application or general-purpose API test files.
 */
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  getAssessorOutcome,
  getAssessorSession,
  recordAssessorOutcome,
} from '../src/api.ts'
import { AssessorOutcomeDashboard } from '../src/components/AssessorOutcomeDashboard.tsx'

afterEach(() => {
  // Every case installs its own network boundary. Clearing it prevents one
  // assessor credential or response from influencing another test.
  vi.unstubAllGlobals()
})

describe('human assessor surface', () => {
  it('starts locked and explains the credential and outcome boundary', () => {
    const page = renderToStaticMarkup(<AssessorOutcomeDashboard />)

    expect(page).toContain('Assessor outcome console')
    expect(page).toContain('separate human-assessor key')
    expect(page).not.toContain('Approved')
    expect(page).not.toContain('Rejected')
  })
})

describe('human assessor API', () => {
  const outcome = {
    outcome_id: '11111111-1111-4111-8111-111111111111',
    claim_id: 4,
    revision: 1,
    outcome: 'ConfirmedFraud' as const,
    assessor_reference: 'research-assessor-1',
    notes: 'Reviewed synthetic evidence.',
    assessed_at: '2026-08-12T12:00:00Z',
  }

  it('authenticates with the dedicated assessor header', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ assessor_reference: 'research-assessor-1' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(getAssessorSession('human-review-key')).resolves.toEqual({
      assessor_reference: 'research-assessor-1',
    })
    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/assessor/session',
      expect.objectContaining({
        headers: { 'X-Assessor-API-Key': 'human-review-key' },
      }),
    )
  })

  it('treats a missing human conclusion as normal pending review', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(null, { status: 404 })),
    )

    await expect(
      getAssessorOutcome(4, 'human-review-key'),
    ).resolves.toBeNull()
  })

  it('records only the human outcome and notes off-chain', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(outcome), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      recordAssessorOutcome(
        4,
        { outcome: 'ConfirmedFraud', notes: 'Reviewed synthetic evidence.' },
        'human-review-key',
      ),
    ).resolves.toEqual(outcome)
    const [url, request] = fetchMock.mock.calls[0]
    expect(url).toBe('http://127.0.0.1:8000/assessor/claims/4/outcome')
    expect(request).toMatchObject({
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Assessor-API-Key': 'human-review-key',
      },
    })
    expect(JSON.parse(request.body)).toEqual({
      outcome: 'ConfirmedFraud',
      notes: 'Reviewed synthetic evidence.',
    })
  })

  it('rejects a business disposition presented as a fraud outcome', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ ...outcome, outcome: 'Rejected' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )

    await expect(getAssessorOutcome(4, 'human-review-key')).rejects.toThrow(
      'unexpected assessor outcome',
    )
  })
})
