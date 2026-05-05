export default {
  async fetch(request, env, ctx) {
    // ===== Mode 1: host -> host =====
    // incoming host -> upstream host (or full URL)
    const DOMAIN_MAP = {
      // Telegram Bot API
      "api.telegram.bot.luoh.org": "api.telegram.org",

      // Discord API / Gateway / CDN
      "api.discord.bot.luoh.org": "discord.com",
      "gateway.discord.bot.luoh.org": "gateway.discord.gg",
      "cdn.discord.bot.luoh.org": "cdn.discordapp.com",
      "media.discord.bot.luoh.org": "media.discordapp.net",
    };

    // ===== Mode 2: wildcard subdomain -> host =====
    // Anything matching `<X>.bot.luoh.org` whose stem ends with one of
    // WILDCARD_TARGETS is proxied to the stem itself. Two-level subdomains
    // are NOT covered by Universal SSL, so this mode requires Total TLS.
    const WILDCARD_SUFFIX = ".bot.luoh.org";
    const WILDCARD_TARGETS = [];

    // ===== Mode 3: single-host + path-based =====
    // Request: https://ytproxy.luoh.org/<upstream_host>/<path>
    //      -> https://<upstream_host>/<path>
    // upstream_host must match one of the allowlisted suffixes for that
    // incoming host. This works with Universal SSL (single-level subdomain).
    const PATH_PROXY_HOSTS = {
      "ytproxy.luoh.org": ["youtube.com", "googlevideo.com", "ytimg.com", "youtu.be"],
    };

    const incomingUrl = new URL(request.url);
    const incomingHost = incomingUrl.hostname;

    let target = null;
    let pathProxyPrefix = ""; // "/music.youtube.com" etc. — to strip from outgoing path
    let pathProxyAllowed = null; // allowlist, for rewriting Location on redirect

    // Try each mode in order:
    target = resolveTarget(DOMAIN_MAP[incomingHost]);

    if (!target) {
      const stem = resolveWildcard(incomingHost, WILDCARD_SUFFIX, WILDCARD_TARGETS);
      if (stem) target = resolveTarget(stem);
    }

    if (!target && PATH_PROXY_HOSTS[incomingHost]) {
      const allowed = PATH_PROXY_HOSTS[incomingHost];
      const parsed = parsePathProxy(incomingUrl.pathname, allowed);
      if (parsed) {
        target = resolveTarget(parsed.upstreamHost);
        pathProxyPrefix = "/" + parsed.upstreamHost;
        pathProxyAllowed = allowed;
      }
    }

    if (!target) {
      return json(
        {
          error: "No proxy rule matched",
          host: incomingHost,
          modes: {
            domainMap: Object.keys(DOMAIN_MAP),
            wildcard: { suffix: WILDCARD_SUFFIX, allowed: WILDCARD_TARGETS },
            pathProxy: PATH_PROXY_HOSTS,
          },
          hint:
            PATH_PROXY_HOSTS[incomingHost]
              ? "Path format: https://" + incomingHost + "/<upstream_host>/<path>"
              : undefined,
        },
        403,
        buildCorsHeaders(request),
      );
    }

    // CORS preflight.
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: buildCorsHeaders(request),
      });
    }

    const upstreamUrl = buildUpstreamUrl(incomingUrl, target, pathProxyPrefix);

    try {
      if (isWebSocketUpgrade(request)) {
        const wsRequest = new Request(upstreamUrl.toString(), {
          method: "GET",
          headers: buildProxyHeaders(request.headers, {
            websocket: true,
            incomingHost,
            incomingProto: incomingUrl.protocol,
          }),
        });
        return await fetch(wsRequest);
      }

      const init = {
        method: request.method,
        headers: buildProxyHeaders(request.headers, {
          websocket: false,
          incomingHost,
          incomingProto: incomingUrl.protocol,
        }),
        redirect: "manual",
      };

      if (!["GET", "HEAD"].includes(request.method)) {
        init.body = request.body;
      }

      const upstreamRequest = new Request(upstreamUrl.toString(), init);
      const upstreamResponse = await fetch(upstreamRequest);

      const responseHeaders = new Headers(upstreamResponse.headers);
      rewriteLocationHeader(responseHeaders, {
        incomingUrl,
        upstreamUrl,
        targetHost: target.host,
        pathProxyAllowed,
      });
      appendCorsHeaders(responseHeaders, request);

      return new Response(upstreamResponse.body, {
        status: upstreamResponse.status,
        statusText: upstreamResponse.statusText,
        headers: responseHeaders,
      });
    } catch (err) {
      return json(
        {
          error: "Upstream fetch failed",
          host: incomingHost,
          target: target.host,
          detail: err instanceof Error ? err.message : String(err),
        },
        502,
        buildCorsHeaders(request),
      );
    }
  },
};

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "proxy-connection",
  "keep-alive",
  "transfer-encoding",
  "te",
  "trailer",
  "upgrade",
]);

function resolveTarget(raw) {
  if (!raw) {
    return null;
  }

  let parsed;
  try {
    parsed = raw.includes("://") ? new URL(raw) : new URL(`https://${raw}`);
  } catch {
    return null;
  }

  let protocol = parsed.protocol;
  if (protocol === "ws:" || protocol === "wss:") {
    protocol = "https:";
  }
  if (protocol !== "http:" && protocol !== "https:") {
    protocol = "https:";
  }

  return {
    protocol,
    host: parsed.hostname,
    port: parsed.port || "",
    basePath: parsed.pathname === "/" ? "" : parsed.pathname.replace(/\/+$/, ""),
  };
}

function resolveWildcard(host, suffix, targets) {
  if (!host.endsWith(suffix)) {
    return null;
  }
  const stem = host.slice(0, -suffix.length);
  for (const t of targets) {
    if (stem === t || stem.endsWith("." + t)) {
      return stem;
    }
  }
  return null;
}

function parsePathProxy(pathname, allowedSuffixes) {
  const match = pathname.match(/^\/([^/]+)(\/.*)?$/);
  if (!match) return null;
  const candidate = match[1].toLowerCase();
  if (!/^[a-z0-9.\-]+$/.test(candidate) || !candidate.includes(".")) {
    return null; // not a plausible hostname
  }
  for (const t of allowedSuffixes) {
    if (candidate === t || candidate.endsWith("." + t)) {
      return { upstreamHost: candidate, rest: match[2] || "/" };
    }
  }
  return null;
}

function buildUpstreamUrl(incomingUrl, target, stripPrefix = "") {
  const upstream = new URL(incomingUrl.toString());
  upstream.protocol = target.protocol;
  upstream.hostname = target.host;
  upstream.port = target.port;

  let path = incomingUrl.pathname;
  if (stripPrefix && path.startsWith(stripPrefix)) {
    path = path.slice(stripPrefix.length) || "/";
  }
  upstream.pathname = joinPaths(target.basePath, path);
  return upstream;
}

function joinPaths(prefix, suffix) {
  const left = prefix ? prefix.replace(/\/+$/, "") : "";
  const right = suffix ? suffix.replace(/^\/+/, "") : "";
  if (!left && !right) {
    return "/";
  }
  if (!left) {
    return `/${right}`;
  }
  if (!right) {
    return left || "/";
  }
  return `${left}/${right}`;
}

function isWebSocketUpgrade(request) {
  const upgrade = request.headers.get("Upgrade");
  return Boolean(upgrade && upgrade.toLowerCase() === "websocket");
}

function buildProxyHeaders(headers, { websocket, incomingHost, incomingProto }) {
  const outgoing = new Headers(headers);

  // Host is derived from target URL in fetch(); remove any incoming host.
  outgoing.delete("host");
  outgoing.set("x-forwarded-host", incomingHost);
  outgoing.set("x-forwarded-proto", incomingProto.replace(":", ""));

  // Remove Cloudflare edge headers that can confuse some upstreams.
  outgoing.delete("cf-connecting-ip");
  outgoing.delete("cf-ipcountry");
  outgoing.delete("cf-ray");
  outgoing.delete("cf-visitor");

  if (!websocket) {
    for (const h of HOP_BY_HOP_HEADERS) {
      outgoing.delete(h);
    }
  }

  return outgoing;
}

function rewriteLocationHeader(headers, { incomingUrl, upstreamUrl, targetHost, pathProxyAllowed }) {
  const rawLocation = headers.get("Location");
  if (!rawLocation) {
    return;
  }

  let location;
  try {
    location = new URL(rawLocation, upstreamUrl.toString());
  } catch {
    return;
  }

  // Path-proxy mode: if upstream redirects to any allowlisted host (including
  // a different one like www.youtube.com -> music.youtube.com), rewrite it so
  // the client keeps going through us instead of bypassing the proxy.
  if (pathProxyAllowed) {
    const h = location.hostname;
    const allowed = pathProxyAllowed.some(
      (t) => h === t || h.endsWith("." + t),
    );
    if (allowed) {
      const newPath = "/" + h + (location.pathname === "/" ? "" : location.pathname);
      const rebuilt = new URL(incomingUrl.toString());
      rebuilt.pathname = newPath;
      rebuilt.search = location.search;
      rebuilt.hash = location.hash;
      headers.set("Location", rebuilt.toString());
    }
    return;
  }

  // Domain-map / wildcard mode: only rewrite when upstream redirects to the
  // same target host.
  if (location.hostname !== targetHost) {
    return;
  }
  location.protocol = incomingUrl.protocol;
  location.hostname = incomingUrl.hostname;
  location.port = incomingUrl.port;
  headers.set("Location", location.toString());
}

function buildCorsHeaders(request) {
  const origin = request.headers.get("Origin") || "*";
  return new Headers({
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD",
    "Access-Control-Allow-Headers": request.headers.get("Access-Control-Request-Headers") || "*",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin, Access-Control-Request-Headers",
  });
}

function appendCorsHeaders(headers, request) {
  const cors = buildCorsHeaders(request);
  for (const [k, v] of cors.entries()) {
    headers.set(k, v);
  }
}

function json(payload, status = 200, extraHeaders = new Headers()) {
  const headers = new Headers(extraHeaders);
  headers.set("Content-Type", "application/json; charset=utf-8");
  return new Response(JSON.stringify(payload, null, 2), { status, headers });
}
