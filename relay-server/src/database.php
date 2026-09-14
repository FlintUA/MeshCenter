<?php

declare(strict_types=1);

function mca_database(array $config): PDO
{
    static $pdo = null;
    if ($pdo instanceof PDO) {
        return $pdo;
    }

    $database = $config['database'] ?? null;
    if (!is_array($database)) {
        throw new RuntimeException('Database configuration is missing.');
    }

    $dsn = sprintf(
        'mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4',
        (string) $database['host'],
        (int) $database['port'],
        (string) $database['name']
    );

    $pdo = new PDO($dsn, (string) $database['user'], (string) $database['password'], [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        PDO::ATTR_EMULATE_PREPARES => false,
        PDO::ATTR_STRINGIFY_FETCHES => false,
    ]);
    $pdo->exec("SET time_zone = '+00:00'");
    return $pdo;
}

function mca_install_schema(PDO $pdo): void
{
    $statements = [
        "CREATE TABLE IF NOT EXISTS mca_meta (
            meta_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            meta_value TEXT NOT NULL,
            PRIMARY KEY (meta_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",

        "CREATE TABLE IF NOT EXISTS mca_transfers (
            upload_id VARCHAR(43) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            transfer_id CHAR(22) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            upload_token_hash BINARY(32) NOT NULL,
            revoke_token_hash BINARY(32) NOT NULL,
            total_size BIGINT UNSIGNED NOT NULL,
            ciphertext_sha256 BINARY(32) NOT NULL,
            manifest_size INT UNSIGNED NOT NULL,
            manifest_sha256 BINARY(32) NOT NULL,
            manifest_uploaded TINYINT(1) NOT NULL DEFAULT 0,
            state VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            hard_ttl_seconds INT UNSIGNED NOT NULL,
            grace_seconds INT UNSIGNED NOT NULL,
            session_expires_at DATETIME NOT NULL,
            hard_expires_at DATETIME NULL,
            delete_after DATETIME NULL,
            tombstone_until DATETIME NULL,
            created_at DATETIME NOT NULL,
            last_activity_at DATETIME NOT NULL,
            committed_at DATETIME NULL,
            revoked_at DATETIME NULL,
            client_ip_hash BINARY(32) NOT NULL,
            PRIMARY KEY (upload_id),
            UNIQUE KEY uq_mca_transfer_id (transfer_id),
            KEY ix_mca_state_expiry (state, hard_expires_at),
            KEY ix_mca_delete_after (delete_after),
            KEY ix_mca_tombstone (tombstone_until)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",

        "CREATE TABLE IF NOT EXISTS mca_chunks (
            upload_id VARCHAR(43) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            chunk_index INT UNSIGNED NOT NULL,
            expected_size INT UNSIGNED NOT NULL,
            expected_sha256 BINARY(32) NOT NULL,
            uploaded TINYINT(1) NOT NULL DEFAULT 0,
            uploaded_at DATETIME NULL,
            PRIMARY KEY (upload_id, chunk_index),
            CONSTRAINT fk_mca_chunks_transfer FOREIGN KEY (upload_id)
                REFERENCES mca_transfers(upload_id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",

        "CREATE TABLE IF NOT EXISTS mca_receipts (
            upload_id VARCHAR(43) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            receipt_hash BINARY(32) NOT NULL,
            completed_at DATETIME NULL,
            PRIMARY KEY (upload_id, receipt_hash),
            CONSTRAINT fk_mca_receipts_transfer FOREIGN KEY (upload_id)
                REFERENCES mca_transfers(upload_id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",

        "CREATE TABLE IF NOT EXISTS mca_rate_limits (
            bucket_key BINARY(32) NOT NULL,
            window_id BIGINT UNSIGNED NOT NULL,
            hits INT UNSIGNED NOT NULL,
            expires_at DATETIME NOT NULL,
            PRIMARY KEY (bucket_key, window_id),
            KEY ix_mca_rate_expiry (expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
    ];

    foreach ($statements as $statement) {
        $pdo->exec($statement);
    }

    $stmt = $pdo->prepare(
        'INSERT INTO mca_meta (meta_key, meta_value) VALUES (?, ?) '
        . 'ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)'
    );
    $stmt->execute(['schema_version', '1']);
    $stmt->execute(['quota_lock', '1']);
}
