# Issue #6459 reproduction artifact

Source: https://github.com/nesquena/hermes-webui/issues/6459

## Reproduction

1. Run Hermes Dashboard on its default loopback bind:

   127.0.0.1:9119

2. Publish it through a reverse proxy at a public HTTPS URL, for example:

   https://dashboard.example.com

3. Configure Hermes WebUI:

```yaml
webui:
  dashboard:
    enabled: always
    url: https://dashboard.example.com
```

4. Access Hermes WebUI from a non-loopback browser URL.

## Actual behavior

- The Dashboard link opens https://dashboard.example.com successfully.
- Its aria-label and tooltip still say:

  Dashboard is loopback-only on the server. Either browse from the server itself or restart it with --host 0.0.0.0 (insecure).

## Expected behavior

When status.browser_url is configured and used as the Dashboard link target, WebUI should not display the loopback-only warning.

The Dashboard service should remain loopback-only. This is a browser-link/UI correction only; it must not cause WebUI to probe the public URL or encourage binding Dashboard to 0.0.0.0.
