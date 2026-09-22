# Headless Chromium for page captures, driven over the Playwright protocol by `worker-capture`.
# Keep the tag in step with the `playwright` pin in pyproject.toml (client and server versions must match).
FROM mcr.microsoft.com/playwright:v1.63.0-noble
RUN npm install -g playwright@1.63.0 && npm cache clean --force
USER pwuser
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["node", "-e", "require('net').connect(3000,'127.0.0.1').on('connect',()=>process.exit(0)).on('error',()=>process.exit(1))"]
CMD ["playwright", "run-server", "--port", "3000", "--host", "0.0.0.0"]
