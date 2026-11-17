# Error monitoring

Set `PENSIEVE_GLITCHTIP_DSN` (or `SENTRY_DSN`) to a GlitchTip project DSN. An empty DSN disables reporting. Set `SENTRY_ENVIRONMENT` and `SENTRY_RELEASE` per deployment. Restart the service after changing its environment. The Sentry SDK is required because this application had no supported error-reporting configuration.

Only error reporting is enabled: no tracing, sessions, request bodies, users, breadcrumbs, local variables, or application extras. Exception messages are reduced to the exception type; stack frames remain available for diagnosis. Keep DSNs in deployment settings; never put an administration token into application configuration.

To verify from the application's environment, initialize `configure_monitoring()` from the monitoring module, call `sentry_sdk.capture_message("Team AER monitoring verification", level="info")`, and `sentry_sdk.flush(timeout=10)`. Confirm the returned event ID appears in the project in GlitchTip. Verification events do not establish that an older deployed release has this integration; deploy and verify each running service separately.
