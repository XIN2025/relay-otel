const loopbackHosts = new Set(["127.0.0.1", "::1", "localhost"]);

function localAuthority(authority: string | null) {
  if (!authority) return null;
  try {
    const url = new URL(`http://${authority}`);
    return loopbackHosts.has(url.hostname) &&
      !url.username &&
      !url.password &&
      url.pathname === "/" &&
      !url.search &&
      !url.hash
      ? url
      : null;
  } catch {
    return null;
  }
}

export function isTrustedLocalRequest(request: Request): boolean {
  // Next may rewrite request.url to localhost; Host keeps what the browser used.
  const target = localAuthority(request.headers.get("host"));
  if (!target) return false;

  const origin = request.headers.get("origin");
  // Launches a local process, so require fetch's same-origin headers.
  if (!origin) return false;
  const fetchSite = request.headers.get("sec-fetch-site");
  if (fetchSite && fetchSite !== "same-origin") return false;
  try {
    const source = new URL(origin);
    return (
      source.protocol === "http:" &&
      loopbackHosts.has(source.hostname) &&
      source.hostname === target.hostname &&
      source.port === target.port
    );
  } catch {
    return false;
  }
}
