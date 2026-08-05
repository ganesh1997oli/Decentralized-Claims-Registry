-- Search support for the authenticated indexer event audit stream.
--
-- The existing claim-history and deployment-order indexes already cover exact
-- claim searches and the unfiltered newest-first stream. These focused indexes
-- keep the additional operator filters predictable as event history grows.

CREATE INDEX claim_index_events_type_order_idx
    ON claim_index_events (
        chain_id, contract_address, event_type,
        block_number DESC, log_index DESC, event_id DESC
    );

CREATE INDEX claim_index_events_status_order_idx
    ON claim_index_events (
        chain_id, contract_address, status,
        block_number DESC, log_index DESC, event_id DESC
    );

CREATE INDEX claim_index_events_transaction_idx
    ON claim_index_events (
        chain_id, contract_address, transaction_hash
    );
