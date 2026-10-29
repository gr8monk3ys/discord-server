import type { NextConfig } from "next";
import { ASSET_CSP, INVITE_URL, SECURITY_HEADERS } from "./src/lib/site";

const list = (h: Record<string, string>) => Object.entries(h).map(([key, value]) => ({ key, value }));

const nextConfig: NextConfig = {
  poweredByHeader: false,
  trailingSlash: false,
  async redirects() {
    // /join is the short link to the server: straight to the invite when one is set up.
    // Without a valid code the /join page explains instead (src/app/join/page.tsx).
    return INVITE_URL ? [{ source: "/join", destination: INVITE_URL, permanent: false }] : [];
  },
  async headers() {
    return [
      { source: "/:path*", headers: list(SECURITY_HEADERS) },
      {
        source: "/img/:path*",
        headers: list({
          "Content-Security-Policy": ASSET_CSP,
          "Cache-Control": "public, max-age=2592000, stale-while-revalidate=86400",
        }),
      },
      { source: "/join", headers: list({ "X-Robots-Tag": "noindex, follow" }) },
      { source: "/(appeal|linked-role)", headers: list({ "X-Robots-Tag": "noindex, nofollow" }) },
      { source: "/api/:path*", headers: list({ "X-Robots-Tag": "noindex, nofollow" }) },
    ];
  },
};

export default nextConfig;
