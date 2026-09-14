<?php

declare(strict_types=1);

final class McaHttpException extends RuntimeException
{
    public int $status;
    public string $errorCode;
    public array $details;

    public function __construct(int $status, string $errorCode, string $message, array $details = [])
    {
        parent::__construct($message);
        $this->status = $status;
        $this->errorCode = $errorCode;
        $this->details = $details;
    }
}

function mca_install_exception_handler(): void
{
    set_exception_handler(static function (Throwable $error): void {
        $requestId = mca_request_id();

        if ($error instanceof McaHttpException) {
            mca_json([
                'ok' => false,
                'error' => $error->errorCode,
                'message' => $error->getMessage(),
                'details' => (object) $error->details,
                'request_id' => $requestId,
            ], $error->status);
        }

        error_log(sprintf(
            'MCAttach Relay request=%s unhandled=%s file=%s line=%d',
            $requestId,
            get_class($error),
            basename($error->getFile()),
            $error->getLine()
        ));

        mca_json([
            'ok' => false,
            'error' => 'internal_error',
            'message' => 'The Relay could not process the request.',
            'request_id' => $requestId,
        ], 500);
    });
}

function mca_request_id(): string
{
    static $requestId = null;
    if ($requestId === null) {
        $requestId = bin2hex(random_bytes(8));
    }
    return $requestId;
}

function mca_request_path(): string
{
    $uri = (string) ($_SERVER['REQUEST_URI'] ?? '/');
    $path = parse_url($uri, PHP_URL_PATH);
    if (!is_string($path)) {
        throw new McaHttpException(400, 'invalid_path', 'Invalid request path.');
    }

    $decoded = rawurldecode($path);
    if (str_contains($decoded, "\0") || str_contains($decoded, '..')) {
        throw new McaHttpException(400, 'invalid_path', 'Invalid request path.');
    }

    if ($decoded !== '/') {
        $decoded = rtrim($decoded, '/');
    }
    return $decoded === '' ? '/' : $decoded;
}

function mca_is_https(): bool
{
    return isset($_SERVER['HTTPS']) && strtolower((string) $_SERVER['HTTPS']) !== 'off';
}

function mca_security_headers(bool $html = false): void
{
    header('X-Content-Type-Options: nosniff');
    header('X-Frame-Options: DENY');
    header('Referrer-Policy: no-referrer');
    header('Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()');
    header('Cache-Control: no-store, max-age=0');

    if ($html) {
        header("Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'");
    } else {
        header("Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'");
    }

    if (mca_is_https()) {
        header('Strict-Transport-Security: max-age=31536000');
    }
}

function mca_json(array $payload, int $status = 200): never
{
    mca_security_headers(false);
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('X-MCA-Request-ID: ' . mca_request_id());
    echo json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    exit;
}

function mca_html(string $body, int $status = 200): never
{
    mca_security_headers(true);
    http_response_code($status);
    header('Content-Type: text/html; charset=utf-8');
    echo $body;
    exit;
}

function mca_escape(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

function mca_page(string $title, string $content): string
{
    $safeTitle = mca_escape($title);
    return '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        . '<meta name="viewport" content="width=device-width,initial-scale=1">'
        . '<title>' . $safeTitle . '</title><style>'
        . ':root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0d1724;color:#e8f0f8;font:16px/1.5 system-ui,sans-serif}'
        . 'main{max-width:760px;margin:6vh auto;padding:28px;background:#162638;border:1px solid #30465e;border-radius:16px}'
        . 'h1{margin-top:0;font-size:1.65rem}.muted{color:#a9bdd0}.ok{color:#75dea0}.warn{color:#ffd37a}.error{color:#ff9e9e}'
        . 'label{display:block;margin:15px 0 5px}input{width:100%;padding:11px;border:1px solid #45617c;border-radius:8px;background:#0d1724;color:#fff}'
        . 'button,.button{display:inline-block;margin-top:20px;padding:11px 18px;border:0;border-radius:9px;background:#68a9ff;color:#061526;font-weight:700;text-decoration:none;cursor:pointer}'
        . 'code,pre{background:#0b1420;border:1px solid #30465e;border-radius:8px;padding:3px 6px}pre{padding:14px;overflow:auto;white-space:pre-wrap}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}'
        . '@media(max-width:620px){main{margin:0;min-height:100vh;border-radius:0}.grid{grid-template-columns:1fr}}'
        . '</style></head><body><main><h1>' . $safeTitle . '</h1>' . $content . '</main></body></html>';
}

function mca_setup_required_page(): never
{
    $content = '<p>Сайт MCAttach Relay создан, но установка ещё не завершена.</p>'
        . '<p class="muted">Для продолжения потребуется ключ установки и реквизиты отдельной базы MySQL.</p>'
        . '<a class="button" href="/setup">Начать установку</a>';
    mca_html(mca_page('MCAttach Relay', $content), 503);
}

function mca_base64url_encode(string $bytes): string
{
    return rtrim(strtr(base64_encode($bytes), '+/', '-_'), '=');
}

function mca_base64url_decode(string $value, ?int $expectedLength = null): string
{
    if ($value === '' || preg_match('/^[A-Za-z0-9_-]+$/D', $value) !== 1) {
        throw new McaHttpException(400, 'invalid_base64url', 'Invalid Base64URL value.');
    }

    $padding = (4 - strlen($value) % 4) % 4;
    $decoded = base64_decode(strtr($value . str_repeat('=', $padding), '-_', '+/'), true);
    if (!is_string($decoded) || ($expectedLength !== null && strlen($decoded) !== $expectedLength)) {
        throw new McaHttpException(400, 'invalid_base64url', 'Invalid Base64URL value length.');
    }
    return $decoded;
}

function mca_hex_hash(string $value, string $field): string
{
    if (preg_match('/^[a-f0-9]{64}$/D', $value) !== 1) {
        throw new McaHttpException(422, 'invalid_' . $field, $field . ' must be a lowercase SHA-256 hex digest.');
    }
    $raw = hex2bin($value);
    if (!is_string($raw)) {
        throw new McaHttpException(422, 'invalid_' . $field, 'Invalid SHA-256 digest.');
    }
    return $raw;
}

function mca_read_json(int $maxBytes = 65536): array
{
    $declared = isset($_SERVER['CONTENT_LENGTH']) ? (int) $_SERVER['CONTENT_LENGTH'] : null;
    if ($declared !== null && $declared > $maxBytes) {
        throw new McaHttpException(413, 'request_too_large', 'JSON request body is too large.');
    }

    $stream = fopen('php://input', 'rb');
    if ($stream === false) {
        throw new RuntimeException('Unable to read request body.');
    }
    $raw = stream_get_contents($stream, $maxBytes + 1);
    fclose($stream);

    if (!is_string($raw) || strlen($raw) > $maxBytes) {
        throw new McaHttpException(413, 'request_too_large', 'JSON request body is too large.');
    }

    try {
        $decoded = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
    } catch (JsonException $error) {
        throw new McaHttpException(400, 'invalid_json', 'Request body must contain valid JSON.');
    }

    if (!is_array($decoded)) {
        throw new McaHttpException(400, 'invalid_json', 'JSON root must be an object.');
    }
    return $decoded;
}

function mca_bearer_token(): string
{
    $header = (string) (
        $_SERVER['HTTP_AUTHORIZATION']
        ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION']
        ?? $_SERVER['HTTP_X_MCA_TOKEN']
        ?? ''
    );

    if (preg_match('/^Bearer\s+([^\s]+)$/iD', trim($header), $matches) !== 1) {
        throw new McaHttpException(401, 'authentication_required', 'A bearer token is required.');
    }
    return $matches[1];
}

function mca_require_token_hash(string $expectedHash): string
{
    $token = mca_bearer_token();
    if (!hash_equals($expectedHash, hash('sha256', $token))) {
        throw new McaHttpException(401, 'invalid_token', 'The supplied token is not valid.');
    }
    return $token;
}

function mca_utc_now(): string
{
    return gmdate('Y-m-d H:i:s');
}

function mca_utc_after(int $seconds): string
{
    return gmdate('Y-m-d H:i:s', time() + $seconds);
}

function mca_iso_utc(?string $mysqlDate): ?string
{
    if ($mysqlDate === null || $mysqlDate === '') {
        return null;
    }
    return str_replace(' ', 'T', $mysqlDate) . 'Z';
}

function mca_require_configured_host(array $config): void
{
    $expected = strtolower((string) parse_url((string) ($config['base_url'] ?? ''), PHP_URL_HOST));
    $actual = strtolower((string) ($_SERVER['HTTP_HOST'] ?? ''));
    $actual = preg_replace('/:\d+$/D', '', $actual) ?? $actual;

    if ($expected !== '' && $actual !== '' && !hash_equals($expected, $actual)) {
        throw new McaHttpException(421, 'wrong_host', 'This hostname is not configured for the Relay.');
    }
}

function mca_canonical_json(array $value): string
{
    $normalize = static function ($item) use (&$normalize) {
        if (!is_array($item)) {
            return $item;
        }

        if (!array_is_list($item)) {
            ksort($item, SORT_STRING);
        }
        foreach ($item as $key => $child) {
            $item[$key] = $normalize($child);
        }
        return $item;
    };

    return json_encode(
        $normalize($value),
        JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR
    );
}

function mca_provider_id(string $baseUrl, string $publicKeyBase64url): string
{
    $origin = strtolower(rtrim($baseUrl, '/'));
    $publicKey = mca_base64url_decode($publicKeyBase64url, SODIUM_CRYPTO_SIGN_PUBLICKEYBYTES);
    $digest = hash('sha256', $origin . "\n" . $publicKey, true);
    return mca_base64url_encode(substr($digest, 0, 8));
}
