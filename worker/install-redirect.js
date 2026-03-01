/**
 * Cloudflare Worker: synix.dev/proxy → latest install.sh from GitHub Releases
 *
 * Fetches the latest release tag from the GitHub API, then redirects to
 * the raw install.sh at that tag. Caches the tag lookup for 5 minutes
 * so we don't hammer GitHub on every curl.
 *
 * Deploy:
 *   npx wrangler deploy worker/install-redirect.js --name synix-proxy-installer
 *
 * Route:
 *   synix.dev/proxy  →  this worker
 */

const REPO = "marklubin/double-buffer-proxy";
const CACHE_TTL = 300; // 5 minutes

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Only handle /proxy path
    if (url.pathname !== "/proxy") {
      return new Response("Not found", { status: 404 });
    }

    // Try to get cached tag
    const cache = caches.default;
    const cacheKey = new Request("https://synix.dev/_cache/latest-tag");
    let tag = null;

    const cached = await cache.match(cacheKey);
    if (cached) {
      tag = await cached.text();
    }

    if (!tag) {
      // Fetch latest release from GitHub API
      const resp = await fetch(
        `https://api.github.com/repos/${REPO}/releases/latest`,
        {
          headers: {
            "User-Agent": "synix-proxy-installer/1.0",
            Accept: "application/vnd.github.v3+json",
          },
        }
      );

      if (resp.ok) {
        const data = await resp.json();
        tag = data.tag_name; // e.g. "v1.2.3"
      }

      if (tag) {
        // Cache for 5 minutes
        const cacheResp = new Response(tag, {
          headers: { "Cache-Control": `public, max-age=${CACHE_TTL}` },
        });
        ctx.waitUntil(cache.put(cacheKey, cacheResp));
      }
    }

    // Fallback to main if no release exists yet
    const ref = tag || "main";
    const installUrl = `https://raw.githubusercontent.com/${REPO}/${ref}/install.sh`;

    // Fetch and stream the script (don't redirect — curl follows redirects
    // but some shells don't handle 302 with piped sh)
    const scriptResp = await fetch(installUrl);
    if (!scriptResp.ok) {
      return new Response(`Failed to fetch install script (${ref})`, {
        status: 502,
      });
    }

    return new Response(scriptResp.body, {
      headers: {
        "Content-Type": "text/plain; charset=utf-8",
        "X-Synix-Version": ref,
      },
    });
  },
};
