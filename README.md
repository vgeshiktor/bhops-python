# bhops-python

## Microsoft Graph auth for `salaryops`

`salaryops/salary_publisher.py` now uses MSAL public-client auth for delegated Microsoft Graph mail access.

Set:
- `MS_CLIENT_ID`: Azure app registration client ID
- `MS_AUTHORITY`: optional tenant/authority, defaults to `consumers`
- `MS_TOKEN_CACHE_PATH`: optional persistent token cache path, defaults to `~/.msal_token_cache.bin`
- `MS_INTERACTIVE_AUTH`: optional bootstrap toggle for unattended environments, accepts `1`, `true`, `yes`, `on`

First-time bootstrap:
```bash
python3 salaryops/salary_publisher.py --config /path/to/config.json --interactive-auth
```

Later unattended runs:
```bash
python3 salaryops/salary_publisher.py --config /path/to/config.json
```

The first interactive run stores the MSAL cache on disk. Later runs reuse the same cache path and fail fast with `AUTH_REQUIRED` if the cache is missing or no silent token can be acquired.

Azure app registration prerequisites:
- Enable public client flows for the app registration
- Grant delegated Microsoft Graph permissions required by your workflow, typically `Mail.Read` and `Mail.Send`
- Use an authority compatible with the account type you sign into, such as `consumers` for personal Microsoft accounts
