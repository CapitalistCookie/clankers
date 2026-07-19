# Vendored third-party assets

Served locally (not from a CDN) so the dashboard has no external runtime
dependency and loads instantly even on slow/offline networks.

| File | Package | Version | License |
|------|---------|---------|---------|
| `xterm.min.js`, `xterm.min.css` | [@xterm/xterm](https://github.com/xtermjs/xterm.js) | 5.5.0 | MIT © The xterm.js authors |
| `addon-fit.min.js` | @xterm/addon-fit | 0.10.0 | MIT © The xterm.js authors |
| `addon-webgl.min.js` | @xterm/addon-webgl | 0.18.0 | MIT © The xterm.js authors |
| `addon-web-links.min.js` | @xterm/addon-web-links | 0.11.0 | MIT © The xterm.js authors |
| `qrcode.js` | [qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator) | 1.4.4 | MIT © Kazuhiko Arase |

To update: re-download the pinned version from jsdelivr/npm and replace the file.
