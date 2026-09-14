<?php

declare(strict_types=1);

function mca_objects_root(): string
{
    return MCA_PRIVATE_ROOT . DIRECTORY_SEPARATOR . 'objects';
}

function mca_validate_upload_id(string $uploadId): void
{
    if (preg_match('/^[A-Za-z0-9_-]{43}$/D', $uploadId) !== 1) {
        throw new McaHttpException(400, 'invalid_upload_id', 'Invalid upload session identifier.');
    }
}

function mca_validate_transfer_id(string $transferId): void
{
    if (preg_match('/^[A-Za-z0-9_-]{22}$/D', $transferId) !== 1) {
        throw new McaHttpException(400, 'invalid_transfer_id', 'Invalid transfer identifier.');
    }
    mca_base64url_decode($transferId, 16);
}

function mca_object_directory(string $uploadId): string
{
    mca_validate_upload_id($uploadId);
    return mca_objects_root() . DIRECTORY_SEPARATOR . $uploadId;
}

function mca_chunk_path(string $uploadId, int $index): string
{
    if ($index < 0 || $index > 65535) {
        throw new McaHttpException(400, 'invalid_chunk_index', 'Invalid chunk index.');
    }
    return mca_object_directory($uploadId) . DIRECTORY_SEPARATOR . 'chunk-' . $index . '.bin';
}

function mca_manifest_path(string $uploadId): string
{
    return mca_object_directory($uploadId) . DIRECTORY_SEPARATOR . 'manifest.bin';
}

function mca_ensure_object_directory(string $uploadId): string
{
    $directory = mca_object_directory($uploadId);
    if (!is_dir($directory) && !mkdir($directory, 0700, true) && !is_dir($directory)) {
        throw new RuntimeException('Unable to create object directory.');
    }
    @chmod($directory, 0700);
    return $directory;
}

function mca_store_request_body(string $destination, int $expectedSize, string $expectedHash): array
{
    if ($expectedSize < 0) {
        throw new RuntimeException('Invalid expected object size.');
    }

    if (isset($_SERVER['CONTENT_LENGTH']) && (int) $_SERVER['CONTENT_LENGTH'] !== $expectedSize) {
        throw new McaHttpException(422, 'size_mismatch', 'Request body size does not match the declared size.');
    }

    $directory = dirname($destination);
    if (!is_dir($directory)) {
        throw new RuntimeException('Object directory is missing.');
    }

    $temporary = $directory . DIRECTORY_SEPARATOR . '.part-' . bin2hex(random_bytes(8));
    $input = fopen('php://input', 'rb');
    $output = fopen($temporary, 'xb');
    if ($input === false || $output === false) {
        if (is_resource($input)) {
            fclose($input);
        }
        if (is_resource($output)) {
            fclose($output);
        }
        @unlink($temporary);
        throw new RuntimeException('Unable to open upload streams.');
    }

    $hash = hash_init('sha256');
    $written = 0;

    try {
        while (!feof($input)) {
            $buffer = fread($input, 65536);
            if ($buffer === false) {
                throw new RuntimeException('Unable to read upload stream.');
            }
            if ($buffer === '') {
                continue;
            }

            $written += strlen($buffer);
            if ($written > $expectedSize) {
                throw new McaHttpException(413, 'request_too_large', 'Uploaded object is larger than declared.');
            }

            hash_update($hash, $buffer);
            $remaining = $buffer;
            while ($remaining !== '') {
                $count = fwrite($output, $remaining);
                if ($count === false || $count === 0) {
                    throw new RuntimeException('Unable to write uploaded object.');
                }
                $remaining = (string) substr($remaining, $count);
            }
        }

        if ($written !== $expectedSize) {
            throw new McaHttpException(422, 'size_mismatch', 'Uploaded object size does not match the declaration.');
        }

        $actualHash = hash_final($hash, true);
        if (!hash_equals($expectedHash, $actualHash)) {
            throw new McaHttpException(422, 'digest_mismatch', 'Uploaded object digest does not match the declaration.');
        }

        fflush($output);
        fclose($input);
        fclose($output);
        $input = null;
        $output = null;
        @chmod($temporary, 0600);

        if (!rename($temporary, $destination)) {
            throw new RuntimeException('Unable to publish uploaded object part.');
        }
        @chmod($destination, 0600);

        return ['size' => $written, 'sha256' => bin2hex($actualHash)];
    } finally {
        if (is_resource($input)) {
            fclose($input);
        }
        if (is_resource($output)) {
            fclose($output);
        }
        if (is_file($temporary)) {
            @unlink($temporary);
        }
    }
}

function mca_digest_ordered_chunks(string $uploadId, array $chunks): array
{
    $hash = hash_init('sha256');
    $total = 0;

    foreach ($chunks as $chunk) {
        $index = (int) $chunk['chunk_index'];
        $path = mca_chunk_path($uploadId, $index);
        $expectedSize = (int) $chunk['expected_size'];
        if (!is_file($path) || filesize($path) !== $expectedSize) {
            throw new McaHttpException(409, 'chunk_missing', 'One or more chunks are missing.');
        }

        $stream = fopen($path, 'rb');
        if ($stream === false) {
            throw new RuntimeException('Unable to read stored chunk.');
        }
        $bytes = hash_update_stream($hash, $stream);
        fclose($stream);
        if ($bytes !== $expectedSize) {
            throw new RuntimeException('Stored chunk size changed during commit.');
        }
        $total += $bytes;
    }

    return ['size' => $total, 'sha256' => hash_final($hash, true)];
}

function mca_safe_delete_object_directory(string $uploadId): bool
{
    mca_validate_upload_id($uploadId);
    $root = realpath(mca_objects_root());
    $directory = mca_object_directory($uploadId);

    if ($root === false || !is_dir($directory)) {
        return true;
    }

    $resolved = realpath($directory);
    if ($resolved === false || dirname($resolved) !== $root) {
        throw new RuntimeException('Refusing to delete an unsafe object path.');
    }

    $items = scandir($resolved);
    if ($items === false) {
        return false;
    }

    foreach ($items as $item) {
        if ($item === '.' || $item === '..') {
            continue;
        }
        $path = $resolved . DIRECTORY_SEPARATOR . $item;
        if (is_dir($path)) {
            return false;
        }
        if (!@unlink($path) && is_file($path)) {
            return false;
        }
    }

    return @rmdir($resolved) || !is_dir($resolved);
}

