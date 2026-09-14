# Local and hosting verification

Before packaging:

```sh
find . -name '*.php' -print0 | xargs -0 -n1 php -l
```

After deployment:

```sh
curl -fsS https://mcattach.elektroniker.help/health
curl -fsS https://mcattach.elektroniker.help/v1/info
```

`smoke_test.py` performs a full live API cycle: info, session creation,
resumable status, chunk and manifest upload, commit, download, completion and
revoke. It uses random opaque bytes as test ciphertext and deletes the test
object at the end.

Run from a trusted computer after installation:

```bash
MCA_RELAY_URL=https://mcattach.elektroniker.help \
MCA_UPLOAD_TOKEN='YOUR_TOKEN' \
python3 smoke_test.py
```

The script does not print tokens. It checks that the public key in the
descriptor matches `/v1/info`, but it does not cryptographically verify the
Ed25519 signature because it intentionally has no third-party dependencies.
