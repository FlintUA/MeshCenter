<?php

declare(strict_types=1);

function mca_expire_transfer(PDO $pdo, array $config, string $uploadId): void
{
    mca_validate_upload_id($uploadId);
    $tombstoneUntil = mca_utc_after((int) $config['limits']['tombstone_seconds']);

    $stmt = $pdo->prepare(
        "UPDATE mca_transfers SET state = 'expired', tombstone_until = ?, last_activity_at = ? "
        . "WHERE upload_id = ? AND state IN ('staging', 'committed')"
    );
    $stmt->execute([$tombstoneUntil, mca_utc_now(), $uploadId]);

    mca_safe_delete_object_directory($uploadId);
    $pdo->prepare('DELETE FROM mca_chunks WHERE upload_id = ?')->execute([$uploadId]);
    $pdo->prepare('DELETE FROM mca_receipts WHERE upload_id = ?')->execute([$uploadId]);
}

function mca_cleanup(PDO $pdo, array $config, int $limit = 100): array
{
    $limit = max(1, min($limit, 500));
    $now = mca_utc_now();
    $sql = "SELECT upload_id FROM mca_transfers
            WHERE (state = 'staging' AND session_expires_at <= :now1)
               OR (state = 'committed' AND hard_expires_at IS NOT NULL AND hard_expires_at <= :now2)
               OR (state = 'committed' AND delete_after IS NOT NULL AND delete_after <= :now3)
            ORDER BY created_at ASC LIMIT " . $limit;
    $stmt = $pdo->prepare($sql);
    $stmt->execute(['now1' => $now, 'now2' => $now, 'now3' => $now]);
    $expired = 0;

    foreach ($stmt->fetchAll() as $row) {
        mca_expire_transfer($pdo, $config, (string) $row['upload_id']);
        $expired++;
    }

    $stmt = $pdo->prepare(
        "DELETE FROM mca_transfers WHERE state IN ('expired', 'revoked') "
        . 'AND tombstone_until IS NOT NULL AND tombstone_until <= ?'
    );
    $stmt->execute([$now]);
    $tombstonesDeleted = $stmt->rowCount();

    $stmt = $pdo->prepare('DELETE FROM mca_rate_limits WHERE expires_at <= ?');
    $stmt->execute([$now]);

    return [
        'expired_objects' => $expired,
        'deleted_tombstones' => $tombstonesDeleted,
        'deleted_rate_buckets' => $stmt->rowCount(),
    ];
}

function mca_maybe_cleanup(PDO $pdo, array $config): void
{
    try {
        if (random_int(1, 100) === 1) {
            mca_cleanup($pdo, $config, 20);
        }
    } catch (Throwable $error) {
        error_log('MCAttach Relay opportunistic cleanup failed: ' . get_class($error));
    }
}

