<?php

declare(strict_types=1);

require_once MCA_PUBLIC_ROOT . '/src/storage.php';
require_once MCA_PUBLIC_ROOT . '/src/maintenance.php';

function mca_relay_dispatch(array $config, string $method, string $path): never
{
    if (!mca_is_https()) {
        throw new McaHttpException(400, 'https_required', 'MCAttach Relay accepts requests only over HTTPS.');
    }

    $pdo = mca_database($config);
    mca_maybe_cleanup($pdo, $config);

    if ($method === 'GET' && $path === '/') {
        mca_status_page($pdo, $config);
    }
    if ($method === 'GET' && $path === '/health') {
        mca_health($pdo, $config);
    }
    if ($method === 'GET' && $path === '/v1/info') {
        mca_info($config);
    }
    if ($method === 'POST' && $path === '/v1/uploads') {
        mca_create_upload($pdo, $config);
    }

    if (preg_match('#^/v1/uploads/([A-Za-z0-9_-]{43})$#D', $path, $matches) === 1) {
        if ($method !== 'GET') {
            header('Allow: GET');
            throw new McaHttpException(405, 'method_not_allowed', 'This endpoint requires GET.');
        }
        mca_get_upload_status($pdo, $config, $matches[1]);
    }

    if (preg_match('#^/v1/uploads/([A-Za-z0-9_-]{43})/chunks/([0-9]{1,5})$#D', $path, $matches) === 1) {
        if ($method !== 'PUT') {
            header('Allow: PUT');
            throw new McaHttpException(405, 'method_not_allowed', 'This endpoint requires PUT.');
        }
        mca_upload_chunk($pdo, $config, $matches[1], (int) $matches[2]);
    }

    if (preg_match('#^/v1/uploads/([A-Za-z0-9_-]{43})/manifest$#D', $path, $matches) === 1) {
        if ($method !== 'PUT') {
            header('Allow: PUT');
            throw new McaHttpException(405, 'method_not_allowed', 'This endpoint requires PUT.');
        }
        mca_upload_manifest($pdo, $config, $matches[1]);
    }

    if (preg_match('#^/v1/uploads/([A-Za-z0-9_-]{43})/commit$#D', $path, $matches) === 1) {
        if ($method !== 'POST') {
            header('Allow: POST');
            throw new McaHttpException(405, 'method_not_allowed', 'This endpoint requires POST.');
        }
        mca_commit_upload($pdo, $config, $matches[1]);
    }

    if (preg_match('#^/v1/objects/([A-Za-z0-9_-]{22})/descriptor$#D', $path, $matches) === 1) {
        if ($method !== 'GET') {
            header('Allow: GET');
            throw new McaHttpException(405, 'method_not_allowed', 'This endpoint requires GET.');
        }
        mca_get_descriptor($pdo, $config, $matches[1]);
    }

    if (preg_match('#^/v1/objects/([A-Za-z0-9_-]{22})/chunks/([0-9]{1,5})$#D', $path, $matches) === 1) {
        if ($method !== 'GET') {
            header('Allow: GET');
            throw new McaHttpException(405, 'method_not_allowed', 'This endpoint requires GET.');
        }
        mca_download_chunk($pdo, $config, $matches[1], (int) $matches[2]);
    }

    if (preg_match('#^/v1/objects/([A-Za-z0-9_-]{22})/complete$#D', $path, $matches) === 1) {
        if ($method !== 'POST') {
            header('Allow: POST');
            throw new McaHttpException(405, 'method_not_allowed', 'This endpoint requires POST.');
        }
        mca_complete_object($pdo, $config, $matches[1]);
    }

    if (preg_match('#^/v1/objects/([A-Za-z0-9_-]{22})$#D', $path, $matches) === 1) {
        if ($method !== 'DELETE') {
            header('Allow: DELETE');
            throw new McaHttpException(405, 'method_not_allowed', 'This endpoint requires DELETE.');
        }
        mca_revoke_object($pdo, $config, $matches[1]);
    }

    throw new McaHttpException(404, 'not_found', 'The requested endpoint does not exist.');
}

function mca_status_page(PDO $pdo, array $config): never
{
    $pdo->query('SELECT 1')->fetchColumn();
    $providerId = mca_provider_id((string) $config['base_url'], (string) $config['service_public_key']);
    $maxMiB = round(((int) $config['limits']['max_ciphertext_bytes']) / 1048576, 1);
    $hardHours = round(((int) $config['limits']['default_hard_ttl_seconds']) / 3600, 1);
    $content = '<p class="ok"><strong>Relay работает.</strong></p>'
        . '<p>Сервис принимает только зашифрованные объекты MCA/1. Имена файлов и содержимое Relay не расшифровывает.</p>'
        . '<p>Provider ID:</p><pre>' . mca_escape($providerId) . '</pre>'
        . '<p class="muted">Версия: ' . mca_escape(MCA_RELAY_VERSION)
        . '<br>Максимальный ciphertext: ' . mca_escape((string) $maxMiB) . ' MiB'
        . '<br>Hard expiry по умолчанию: ' . mca_escape((string) $hardHours) . ' ч.</p>';
    mca_html(mca_page('MCAttach Relay', $content));
}

function mca_health(PDO $pdo, array $config): never
{
    $pdo->query('SELECT 1')->fetchColumn();
    mca_json([
        'ok' => true,
        'status' => 'ready',
        'version' => MCA_RELAY_VERSION,
        'provider_id' => mca_provider_id((string) $config['base_url'], (string) $config['service_public_key']),
        'time' => gmdate('Y-m-d\TH:i:s\Z'),
    ]);
}

function mca_info(array $config): never
{
    $limits = $config['limits'];
    mca_json([
        'ok' => true,
        'protocol' => 'MCA/1',
        'relay_version' => MCA_RELAY_VERSION,
        'base_url' => $config['base_url'],
        'provider_id' => mca_provider_id((string) $config['base_url'], (string) $config['service_public_key']),
        'service_key' => [
            'type' => 'Ed25519',
            'public_key' => $config['service_public_key'],
        ],
        'limits' => [
            'max_ciphertext_bytes' => (int) $limits['max_ciphertext_bytes'],
            'max_manifest_bytes' => (int) $limits['max_manifest_bytes'],
            'max_chunk_bytes' => (int) $limits['max_chunk_bytes'],
            'max_chunks' => (int) $limits['max_chunks'],
            'max_recipients' => (int) $limits['max_receipts'],
            'default_hard_ttl_seconds' => (int) $limits['default_hard_ttl_seconds'],
            'max_hard_ttl_seconds' => (int) $limits['max_hard_ttl_seconds'],
            'default_download_grace_seconds' => (int) $limits['default_grace_seconds'],
        ],
        'capabilities' => [
            'chunked_upload' => true,
            'receipt_completion' => true,
            'sender_revoke' => true,
            'anonymous_upload' => false,
            'download_authorization' => 'transfer_capability',
        ],
    ]);
}

function mca_rate_limit(PDO $pdo, array $config, string $action, int $limit, int $windowSeconds): void
{
    $ip = (string) ($_SERVER['REMOTE_ADDR'] ?? 'unknown');
    $secret = mca_base64url_decode((string) $config['rate_limit_secret'], 32);
    $window = intdiv(time(), $windowSeconds);
    $bucket = hash_hmac('sha256', $action . "\n" . $window . "\n" . $ip, $secret, true);
    $expires = gmdate('Y-m-d H:i:s', ($window + 2) * $windowSeconds);

    $stmt = $pdo->prepare(
        'INSERT INTO mca_rate_limits (bucket_key, window_id, hits, expires_at) VALUES (?, ?, 1, ?) '
        . 'ON DUPLICATE KEY UPDATE hits = hits + 1, expires_at = VALUES(expires_at)'
    );
    $stmt->execute([$bucket, $window, $expires]);

    $stmt = $pdo->prepare('SELECT hits FROM mca_rate_limits WHERE bucket_key = ? AND window_id = ?');
    $stmt->execute([$bucket, $window]);
    $hits = (int) $stmt->fetchColumn();
    if ($hits > $limit) {
        $retryAfter = (($window + 1) * $windowSeconds) - time();
        header('Retry-After: ' . max(1, $retryAfter));
        throw new McaHttpException(429, 'rate_limited', 'Too many requests. Try again later.');
    }
}

function mca_client_ip_hash(array $config): string
{
    $ip = (string) ($_SERVER['REMOTE_ADDR'] ?? 'unknown');
    $secret = mca_base64url_decode((string) $config['rate_limit_secret'], 32);
    return hash_hmac('sha256', 'client-ip' . "\n" . gmdate('Y-m-d') . "\n" . $ip, $secret, true);
}

function mca_required_integer(array $data, string $field, int $minimum, int $maximum): int
{
    if (!array_key_exists($field, $data) || !is_int($data[$field])) {
        throw new McaHttpException(422, 'invalid_' . $field, $field . ' must be an integer.');
    }
    $value = $data[$field];
    if ($value < $minimum || $value > $maximum) {
        throw new McaHttpException(422, 'invalid_' . $field, $field . ' is outside the allowed range.');
    }
    return $value;
}

function mca_optional_integer(array $data, string $field, int $default, int $minimum, int $maximum): int
{
    if (!array_key_exists($field, $data)) {
        return $default;
    }
    return mca_required_integer($data, $field, $minimum, $maximum);
}

function mca_create_upload(PDO $pdo, array $config): never
{
    mca_rate_limit($pdo, $config, 'create-upload', 30, 3600);
    mca_require_token_hash((string) $config['upload_access_token_hash']);
    $data = mca_read_json(131072);
    $limits = $config['limits'];

    $transferId = (string) ($data['transfer_id'] ?? '');
    mca_validate_transfer_id($transferId);
    $totalSize = mca_required_integer($data, 'total_size', 1, (int) $limits['max_ciphertext_bytes']);
    $ciphertextHash = mca_hex_hash((string) ($data['ciphertext_sha256'] ?? ''), 'ciphertext_sha256');
    $manifestSize = mca_required_integer($data, 'manifest_size', 1, (int) $limits['max_manifest_bytes']);
    $manifestHash = mca_hex_hash((string) ($data['manifest_sha256'] ?? ''), 'manifest_sha256');
    $hardTtl = mca_optional_integer(
        $data,
        'hard_ttl_seconds',
        (int) $limits['default_hard_ttl_seconds'],
        (int) $limits['min_hard_ttl_seconds'],
        (int) $limits['max_hard_ttl_seconds']
    );
    $grace = mca_optional_integer(
        $data,
        'download_grace_seconds',
        (int) $limits['default_grace_seconds'],
        60,
        (int) $limits['max_grace_seconds']
    );

    $chunks = $data['chunks'] ?? null;
    if (!is_array($chunks) || !array_is_list($chunks) || $chunks === [] || count($chunks) > (int) $limits['max_chunks']) {
        throw new McaHttpException(422, 'invalid_chunks', 'chunks must be a non-empty bounded array.');
    }
    $validatedChunks = [];
    $sum = 0;
    foreach ($chunks as $index => $chunk) {
        if (!is_array($chunk)) {
            throw new McaHttpException(422, 'invalid_chunks', 'Each chunk declaration must be an object.');
        }
        $size = mca_required_integer($chunk, 'size', 1, (int) $limits['max_chunk_bytes']);
        $hash = mca_hex_hash((string) ($chunk['sha256'] ?? ''), 'chunk_sha256');
        $sum += $size;
        if ($sum > $totalSize) {
            throw new McaHttpException(422, 'invalid_chunks', 'Declared chunk sizes exceed total_size.');
        }
        $validatedChunks[] = ['index' => $index, 'size' => $size, 'hash' => $hash];
    }
    if ($sum !== $totalSize) {
        throw new McaHttpException(422, 'invalid_chunks', 'Declared chunk sizes must equal total_size.');
    }

    $receipts = $data['receipt_hashes'] ?? null;
    if (!is_array($receipts) || !array_is_list($receipts) || $receipts === [] || count($receipts) > (int) $limits['max_receipts']) {
        throw new McaHttpException(422, 'invalid_receipts', 'receipt_hashes must contain between 1 and the allowed maximum entries.');
    }
    $validatedReceipts = [];
    foreach ($receipts as $receipt) {
        if (!is_string($receipt)) {
            throw new McaHttpException(422, 'invalid_receipts', 'Each receipt hash must be a SHA-256 hex string.');
        }
        $raw = mca_hex_hash($receipt, 'receipt_hash');
        if (isset($validatedReceipts[$receipt])) {
            throw new McaHttpException(422, 'invalid_receipts', 'Duplicate receipt hashes are not allowed.');
        }
        $validatedReceipts[$receipt] = $raw;
    }

    $uploadId = mca_base64url_encode(random_bytes(32));
    $uploadToken = 'mca_us_' . mca_base64url_encode(random_bytes(32));
    $revokeToken = 'mca_rv_' . mca_base64url_encode(random_bytes(32));
    mca_ensure_object_directory($uploadId);

    try {
        $pdo->beginTransaction();
        $stmt = $pdo->query("SELECT meta_value FROM mca_meta WHERE meta_key = 'quota_lock' FOR UPDATE");
        $stmt->fetchColumn();
        $stmt = $pdo->query(
            "SELECT COALESCE(SUM(total_size + manifest_size), 0) FROM mca_transfers "
            . "WHERE state IN ('staging', 'committed')"
        );
        $usedBytes = (int) $stmt->fetchColumn();
        if ($usedBytes + $totalSize + $manifestSize > (int) $limits['max_storage_bytes']) {
            throw new McaHttpException(507, 'relay_quota_exceeded', 'Relay storage quota would be exceeded.');
        }

        $stmt = $pdo->prepare(
            'INSERT INTO mca_transfers '
            . '(upload_id, transfer_id, upload_token_hash, revoke_token_hash, total_size, ciphertext_sha256, '
            . 'manifest_size, manifest_sha256, state, hard_ttl_seconds, grace_seconds, session_expires_at, '
            . 'created_at, last_activity_at, client_ip_hash) '
            . "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staging', ?, ?, ?, ?, ?, ?)"
        );
        $now = mca_utc_now();
        $sessionExpires = mca_utc_after((int) $limits['upload_session_seconds']);
        $stmt->execute([
            $uploadId,
            $transferId,
            hash('sha256', $uploadToken, true),
            hash('sha256', $revokeToken, true),
            $totalSize,
            $ciphertextHash,
            $manifestSize,
            $manifestHash,
            $hardTtl,
            $grace,
            $sessionExpires,
            $now,
            $now,
            mca_client_ip_hash($config),
        ]);

        $stmt = $pdo->prepare(
            'INSERT INTO mca_chunks (upload_id, chunk_index, expected_size, expected_sha256) VALUES (?, ?, ?, ?)'
        );
        foreach ($validatedChunks as $chunk) {
            $stmt->execute([$uploadId, $chunk['index'], $chunk['size'], $chunk['hash']]);
        }

        $stmt = $pdo->prepare('INSERT INTO mca_receipts (upload_id, receipt_hash) VALUES (?, ?)');
        foreach ($validatedReceipts as $receiptHash) {
            $stmt->execute([$uploadId, $receiptHash]);
        }
        $pdo->commit();
    } catch (Throwable $error) {
        if ($pdo->inTransaction()) {
            $pdo->rollBack();
        }
        mca_safe_delete_object_directory($uploadId);
        if ($error instanceof PDOException && (string) $error->getCode() === '23000') {
            throw new McaHttpException(409, 'transfer_exists', 'This transfer identifier already exists.');
        }
        throw $error;
    }

    mca_json([
        'ok' => true,
        'upload_id' => $uploadId,
        'transfer_id' => $transferId,
        'upload_token' => $uploadToken,
        'revoke_token' => $revokeToken,
        'session_expires_at' => mca_iso_utc($sessionExpires),
        'chunk_count' => count($validatedChunks),
    ], 201);
}

function mca_fetch_upload(PDO $pdo, string $uploadId, bool $forUpdate = false): array
{
    mca_validate_upload_id($uploadId);
    $sql = 'SELECT * FROM mca_transfers WHERE upload_id = ?' . ($forUpdate ? ' FOR UPDATE' : '');
    $stmt = $pdo->prepare($sql);
    $stmt->execute([$uploadId]);
    $row = $stmt->fetch();
    if (!is_array($row)) {
        throw new McaHttpException(404, 'upload_not_found', 'Upload session was not found.');
    }
    return $row;
}

function mca_require_upload_token(array $transfer): void
{
    $token = mca_bearer_token();
    $expected = (string) $transfer['upload_token_hash'];
    if (!hash_equals($expected, hash('sha256', $token, true))) {
        throw new McaHttpException(401, 'invalid_token', 'The supplied upload token is not valid.');
    }
}

function mca_require_staging_upload(array $transfer): void
{
    if ((string) $transfer['state'] !== 'staging') {
        throw new McaHttpException(409, 'upload_not_staging', 'Upload session is no longer writable.');
    }
    if (strtotime((string) $transfer['session_expires_at'] . ' UTC') <= time()) {
        throw new McaHttpException(410, 'upload_expired', 'Upload session has expired.');
    }
}

function mca_get_upload_status(PDO $pdo, array $config, string $uploadId): never
{
    mca_rate_limit($pdo, $config, 'upload-status', 240, 3600);
    $transfer = mca_fetch_upload($pdo, $uploadId);
    mca_require_upload_token($transfer);

    if ((string) $transfer['state'] === 'staging'
        && strtotime((string) $transfer['session_expires_at'] . ' UTC') <= time()
    ) {
        mca_expire_transfer($pdo, $config, $uploadId);
        throw new McaHttpException(410, 'upload_expired', 'Upload session has expired.');
    }

    $stmt = $pdo->prepare(
        'SELECT chunk_index, uploaded FROM mca_chunks WHERE upload_id = ? ORDER BY chunk_index ASC'
    );
    $stmt->execute([$uploadId]);
    $chunks = [];
    foreach ($stmt->fetchAll() as $chunk) {
        $chunks[] = [
            'index' => (int) $chunk['chunk_index'],
            'uploaded' => (int) $chunk['uploaded'] === 1,
        ];
    }

    mca_json([
        'ok' => true,
        'upload_id' => $uploadId,
        'transfer_id' => (string) $transfer['transfer_id'],
        'state' => (string) $transfer['state'],
        'manifest_uploaded' => (int) $transfer['manifest_uploaded'] === 1,
        'chunks' => $chunks,
        'session_expires_at' => mca_iso_utc((string) $transfer['session_expires_at']),
        'hard_expires_at' => mca_iso_utc(
            $transfer['hard_expires_at'] === null ? null : (string) $transfer['hard_expires_at']
        ),
    ]);
}

function mca_upload_chunk(PDO $pdo, array $config, string $uploadId, int $index): never
{
    mca_rate_limit($pdo, $config, 'upload-part', 600, 3600);
    $transfer = mca_fetch_upload($pdo, $uploadId);
    mca_require_upload_token($transfer);
    mca_require_staging_upload($transfer);

    $stmt = $pdo->prepare('SELECT * FROM mca_chunks WHERE upload_id = ? AND chunk_index = ?');
    $stmt->execute([$uploadId, $index]);
    $chunk = $stmt->fetch();
    if (!is_array($chunk)) {
        throw new McaHttpException(404, 'chunk_not_declared', 'This chunk index was not declared.');
    }

    mca_ensure_object_directory($uploadId);
    $result = mca_store_request_body(
        mca_chunk_path($uploadId, $index),
        (int) $chunk['expected_size'],
        (string) $chunk['expected_sha256']
    );

    $stmt = $pdo->prepare(
        'UPDATE mca_chunks SET uploaded = 1, uploaded_at = ? WHERE upload_id = ? AND chunk_index = ?'
    );
    $stmt->execute([mca_utc_now(), $uploadId, $index]);
    $pdo->prepare('UPDATE mca_transfers SET last_activity_at = ? WHERE upload_id = ?')
        ->execute([mca_utc_now(), $uploadId]);

    mca_json(['ok' => true, 'chunk_index' => $index, 'size' => $result['size'], 'sha256' => $result['sha256']]);
}

function mca_upload_manifest(PDO $pdo, array $config, string $uploadId): never
{
    mca_rate_limit($pdo, $config, 'upload-part', 600, 3600);
    $transfer = mca_fetch_upload($pdo, $uploadId);
    mca_require_upload_token($transfer);
    mca_require_staging_upload($transfer);
    mca_ensure_object_directory($uploadId);

    $result = mca_store_request_body(
        mca_manifest_path($uploadId),
        (int) $transfer['manifest_size'],
        (string) $transfer['manifest_sha256']
    );
    $stmt = $pdo->prepare('UPDATE mca_transfers SET manifest_uploaded = 1, last_activity_at = ? WHERE upload_id = ?');
    $stmt->execute([mca_utc_now(), $uploadId]);

    mca_json(['ok' => true, 'size' => $result['size'], 'sha256' => $result['sha256']]);
}

function mca_commit_upload(PDO $pdo, array $config, string $uploadId): never
{
    mca_rate_limit($pdo, $config, 'commit-upload', 120, 3600);
    $pdo->beginTransaction();

    try {
        $transfer = mca_fetch_upload($pdo, $uploadId, true);
        mca_require_upload_token($transfer);

        if ((string) $transfer['state'] === 'committed') {
            $pdo->commit();
            mca_json(mca_descriptor_payload($pdo, $config, $transfer));
        }
        mca_require_staging_upload($transfer);

        if ((int) $transfer['manifest_uploaded'] !== 1 || !is_file(mca_manifest_path($uploadId))) {
            throw new McaHttpException(409, 'manifest_missing', 'Encrypted manifest has not been uploaded.');
        }
        $manifestSize = filesize(mca_manifest_path($uploadId));
        $manifestHash = hash_file('sha256', mca_manifest_path($uploadId), true);
        if ($manifestSize !== (int) $transfer['manifest_size']
            || !is_string($manifestHash)
            || !hash_equals((string) $transfer['manifest_sha256'], $manifestHash)
        ) {
            throw new McaHttpException(409, 'manifest_invalid', 'Stored manifest failed final verification.');
        }

        $stmt = $pdo->prepare('SELECT * FROM mca_chunks WHERE upload_id = ? ORDER BY chunk_index ASC');
        $stmt->execute([$uploadId]);
        $chunks = $stmt->fetchAll();
        foreach ($chunks as $chunk) {
            if ((int) $chunk['uploaded'] !== 1) {
                throw new McaHttpException(409, 'chunk_missing', 'One or more chunks have not been uploaded.');
            }
        }

        $digest = mca_digest_ordered_chunks($uploadId, $chunks);
        if ($digest['size'] !== (int) $transfer['total_size']
            || !hash_equals((string) $transfer['ciphertext_sha256'], (string) $digest['sha256'])
        ) {
            throw new McaHttpException(409, 'ciphertext_invalid', 'Stored ciphertext failed final verification.');
        }

        $committedAt = mca_utc_now();
        $hardExpiresAt = mca_utc_after((int) $transfer['hard_ttl_seconds']);
        $stmt = $pdo->prepare(
            "UPDATE mca_transfers SET state = 'committed', committed_at = ?, hard_expires_at = ?, "
            . 'last_activity_at = ? WHERE upload_id = ?'
        );
        $stmt->execute([$committedAt, $hardExpiresAt, $committedAt, $uploadId]);
        $pdo->commit();

        $transfer['state'] = 'committed';
        $transfer['committed_at'] = $committedAt;
        $transfer['hard_expires_at'] = $hardExpiresAt;
        mca_json(mca_descriptor_payload($pdo, $config, $transfer));
    } catch (Throwable $error) {
        if ($pdo->inTransaction()) {
            $pdo->rollBack();
        }
        throw $error;
    }
}

function mca_fetch_object(PDO $pdo, array $config, string $transferId): array
{
    mca_validate_transfer_id($transferId);
    $stmt = $pdo->prepare('SELECT * FROM mca_transfers WHERE transfer_id = ?');
    $stmt->execute([$transferId]);
    $transfer = $stmt->fetch();
    if (!is_array($transfer) || (string) $transfer['state'] === 'staging') {
        throw new McaHttpException(404, 'object_not_found', 'Object was not found.');
    }
    if (in_array((string) $transfer['state'], ['expired', 'revoked'], true)) {
        throw new McaHttpException(410, 'object_unavailable', 'Object is no longer available.');
    }

    $hardDue = $transfer['hard_expires_at'] !== null
        && strtotime((string) $transfer['hard_expires_at'] . ' UTC') <= time();
    $graceDue = $transfer['delete_after'] !== null
        && strtotime((string) $transfer['delete_after'] . ' UTC') <= time();
    if ($hardDue || $graceDue) {
        mca_expire_transfer($pdo, $config, (string) $transfer['upload_id']);
        throw new McaHttpException(410, 'object_expired', 'Object has expired.');
    }
    return $transfer;
}

function mca_descriptor_payload(PDO $pdo, array $config, array $transfer): array
{
    $uploadId = (string) $transfer['upload_id'];
    $manifestPath = mca_manifest_path($uploadId);
    $manifest = file_get_contents($manifestPath);
    if (!is_string($manifest) || strlen($manifest) !== (int) $transfer['manifest_size']) {
        throw new RuntimeException('Stored manifest is unavailable.');
    }

    $stmt = $pdo->prepare(
        'SELECT chunk_index, expected_size, expected_sha256 FROM mca_chunks WHERE upload_id = ? ORDER BY chunk_index ASC'
    );
    $stmt->execute([$uploadId]);
    $chunks = [];
    foreach ($stmt->fetchAll() as $chunk) {
        $chunks[] = [
            'index' => (int) $chunk['chunk_index'],
            'size' => (int) $chunk['expected_size'],
            'sha256' => bin2hex((string) $chunk['expected_sha256']),
        ];
    }

    $providerId = mca_provider_id((string) $config['base_url'], (string) $config['service_public_key']);
    $signed = [
        'domain' => 'MCA-RELAY-DESCRIPTOR-V1',
        'protocol' => 'MCA/1',
        'provider_id' => $providerId,
        'transfer_id' => (string) $transfer['transfer_id'],
        'total_size' => (int) $transfer['total_size'],
        'ciphertext_sha256' => bin2hex((string) $transfer['ciphertext_sha256']),
        'manifest_size' => (int) $transfer['manifest_size'],
        'manifest_sha256' => bin2hex((string) $transfer['manifest_sha256']),
        'chunks' => $chunks,
        'committed_at' => mca_iso_utc((string) $transfer['committed_at']),
        'hard_expires_at' => mca_iso_utc((string) $transfer['hard_expires_at']),
        'delete_after' => mca_iso_utc($transfer['delete_after'] === null ? null : (string) $transfer['delete_after']),
    ];

    $secretKey = mca_base64url_decode(
        (string) $config['service_secret_key'],
        SODIUM_CRYPTO_SIGN_SECRETKEYBYTES
    );
    $signature = sodium_crypto_sign_detached(mca_canonical_json($signed), $secretKey);

    return [
        'ok' => true,
        'descriptor' => $signed,
        'encrypted_manifest' => [
            'encoding' => 'base64url',
            'data' => mca_base64url_encode($manifest),
        ],
        'service_signature' => [
            'algorithm' => 'Ed25519',
            'public_key' => $config['service_public_key'],
            'signature' => mca_base64url_encode($signature),
        ],
    ];
}

function mca_get_descriptor(PDO $pdo, array $config, string $transferId): never
{
    mca_rate_limit($pdo, $config, 'download-descriptor', 240, 3600);
    $transfer = mca_fetch_object($pdo, $config, $transferId);
    mca_json(mca_descriptor_payload($pdo, $config, $transfer));
}

function mca_download_chunk(PDO $pdo, array $config, string $transferId, int $index): never
{
    mca_rate_limit($pdo, $config, 'download-chunk', 1200, 3600);
    $transfer = mca_fetch_object($pdo, $config, $transferId);
    $stmt = $pdo->prepare(
        'SELECT expected_size, expected_sha256, uploaded FROM mca_chunks WHERE upload_id = ? AND chunk_index = ?'
    );
    $stmt->execute([(string) $transfer['upload_id'], $index]);
    $chunk = $stmt->fetch();
    if (!is_array($chunk) || (int) $chunk['uploaded'] !== 1) {
        throw new McaHttpException(404, 'chunk_not_found', 'Chunk was not found.');
    }

    $path = mca_chunk_path((string) $transfer['upload_id'], $index);
    if (!is_file($path) || filesize($path) !== (int) $chunk['expected_size']) {
        throw new RuntimeException('Stored chunk is unavailable.');
    }

    mca_security_headers(false);
    http_response_code(200);
    header('Content-Type: application/octet-stream');
    header('Content-Disposition: attachment; filename="chunk-' . $index . '.bin"');
    header('Content-Length: ' . (int) $chunk['expected_size']);
    header('X-MCA-Chunk-SHA256: ' . bin2hex((string) $chunk['expected_sha256']));
    header('ETag: "sha256-' . bin2hex((string) $chunk['expected_sha256']) . '"');
    header('X-MCA-Request-ID: ' . mca_request_id());

    $stream = fopen($path, 'rb');
    if ($stream === false) {
        throw new RuntimeException('Unable to open stored chunk.');
    }
    fpassthru($stream);
    fclose($stream);
    exit;
}

function mca_complete_object(PDO $pdo, array $config, string $transferId): never
{
    mca_rate_limit($pdo, $config, 'complete-object', 120, 3600);
    $transfer = mca_fetch_object($pdo, $config, $transferId);
    $data = mca_read_json(4096);
    $secretText = (string) ($data['receipt_secret'] ?? '');
    $secret = mca_base64url_decode($secretText, 32);
    $receiptHash = hash('sha256', $secret, true);

    $pdo->beginTransaction();
    try {
        $transfer = mca_fetch_upload($pdo, (string) $transfer['upload_id'], true);
        if ((string) $transfer['state'] !== 'committed') {
            throw new McaHttpException(410, 'object_unavailable', 'Object is no longer available.');
        }

        $stmt = $pdo->prepare(
            'SELECT completed_at FROM mca_receipts WHERE upload_id = ? AND receipt_hash = ? FOR UPDATE'
        );
        $stmt->execute([(string) $transfer['upload_id'], $receiptHash]);
        $receipt = $stmt->fetch();
        if (!is_array($receipt)) {
            throw new McaHttpException(403, 'invalid_receipt', 'Receipt proof is not valid for this object.');
        }

        if ($receipt['completed_at'] === null) {
            $stmt = $pdo->prepare(
                'UPDATE mca_receipts SET completed_at = ? WHERE upload_id = ? AND receipt_hash = ?'
            );
            $stmt->execute([mca_utc_now(), (string) $transfer['upload_id'], $receiptHash]);
        }

        $stmt = $pdo->prepare(
            'SELECT COUNT(*) AS total, SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS completed '
            . 'FROM mca_receipts WHERE upload_id = ?'
        );
        $stmt->execute([(string) $transfer['upload_id']]);
        $counts = $stmt->fetch();
        $allCompleted = is_array($counts) && (int) $counts['total'] > 0
            && (int) $counts['total'] === (int) $counts['completed'];

        $deleteAfter = $transfer['delete_after'];
        if ($allCompleted) {
            $candidateTimestamp = time() + (int) $transfer['grace_seconds'];
            $hardTimestamp = strtotime((string) $transfer['hard_expires_at'] . ' UTC');
            $candidateTimestamp = min($candidateTimestamp, $hardTimestamp);
            $candidate = gmdate('Y-m-d H:i:s', $candidateTimestamp);
            if ($deleteAfter === null || strtotime((string) $deleteAfter . ' UTC') > $candidateTimestamp) {
                $deleteAfter = $candidate;
                $pdo->prepare('UPDATE mca_transfers SET delete_after = ?, last_activity_at = ? WHERE upload_id = ?')
                    ->execute([$deleteAfter, mca_utc_now(), (string) $transfer['upload_id']]);
            }
        }

        $pdo->commit();
        mca_json([
            'ok' => true,
            'receipt_recorded' => true,
            'all_recipients_completed' => $allCompleted,
            'delete_after' => mca_iso_utc($deleteAfter === null ? null : (string) $deleteAfter),
        ]);
    } catch (Throwable $error) {
        if ($pdo->inTransaction()) {
            $pdo->rollBack();
        }
        throw $error;
    }
}

function mca_revoke_object(PDO $pdo, array $config, string $transferId): never
{
    mca_rate_limit($pdo, $config, 'revoke-object', 60, 3600);
    mca_validate_transfer_id($transferId);
    $stmt = $pdo->prepare('SELECT * FROM mca_transfers WHERE transfer_id = ?');
    $stmt->execute([$transferId]);
    $transfer = $stmt->fetch();
    if (!is_array($transfer)) {
        throw new McaHttpException(404, 'object_not_found', 'Object was not found.');
    }

    $token = mca_bearer_token();
    if (!hash_equals((string) $transfer['revoke_token_hash'], hash('sha256', $token, true))) {
        throw new McaHttpException(401, 'invalid_token', 'The supplied revoke token is not valid.');
    }

    if ((string) $transfer['state'] !== 'revoked') {
        $now = mca_utc_now();
        $tombstoneUntil = mca_utc_after((int) $config['limits']['tombstone_seconds']);
        $stmt = $pdo->prepare(
            "UPDATE mca_transfers SET state = 'revoked', revoked_at = ?, tombstone_until = ?, "
            . 'last_activity_at = ? WHERE upload_id = ?'
        );
        $stmt->execute([$now, $tombstoneUntil, $now, (string) $transfer['upload_id']]);
        mca_safe_delete_object_directory((string) $transfer['upload_id']);
        $pdo->prepare('DELETE FROM mca_chunks WHERE upload_id = ?')->execute([(string) $transfer['upload_id']]);
        $pdo->prepare('DELETE FROM mca_receipts WHERE upload_id = ?')->execute([(string) $transfer['upload_id']]);
    }

    mca_json(['ok' => true, 'revoked' => true]);
}
