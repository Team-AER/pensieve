# Save to Pensieve (browser extension)

Part of [Pensieve](../README.md), Team AER’s self-hosted RSS reader and saved-page archive.

Saves the page you're on to your Pensieve. Unlike the bookmarklet it can send the page **exactly as you see
it** (after scripts ran, signed in, past a paywall), so the archived copy has what your server alone could not
fetch. Pensieve still keeps a copy of the images, a static page and the article text.

## Install

- **Chrome, Edge, Brave:** open `chrome://extensions`, turn on Developer mode, **Load unpacked**, pick this folder.
- **Firefox (121+):** open `about:debugging#/runtime/this-firefox`, **Load Temporary Add-on**, pick `manifest.json`
  (or package it with `web-ext build` and sign it for a permanent install).

## Set up

1. In Pensieve, open **Manage > Saving and archive** and **Create a token**.
2. The extension's settings page opens on install: enter your Pensieve address (e.g. `https://pensieve.example.com`)
   and the token. The browser asks once for permission to reach that address; it has no access to any other site
   except the tab you save, and only when you click.

## Save a page

Then use the toolbar button (or **Alt+Shift+S**), or right-click a page or a link: **Save to Pensieve**.

It talks to `POST /api/v1/save` with `Authorization: Bearer <token>` and a JSON body of `url`, `title`, `tags`,
`note` and, when "send the page as I see it" is on, `html`.


## Capture behavior

“Send the page as I see it” submits the current tab's DOM, up to an 8 MB HTML limit. If the page is too large
or the browser blocks script access (for example, an internal browser page or PDF viewer), the extension
saves the URL instead. Server-side capture still needs the capture worker and browser; full archive storage
also needs S3 credentials. See the [archive setup](../README.md#saved-links-and-the-page-archive).

Only send rendered HTML to a Pensieve server you trust: it can contain content visible in your signed-in
session. The extension keeps its settings, including the token, in the browser's sync storage. Revoke a
lost token in Pensieve and create another one. The extension uses Pensieve's bundled icons and is covered
by the repository's [MIT License](../LICENSE).
