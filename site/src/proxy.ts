import { NextResponse, type NextRequest } from "next/server";
import { contentSecurityPolicy } from "@/lib/site";

// A fresh nonce per request: Next.js reads it from the request's CSP header and puts it on
// its own inline scripts (the React payload), and the layout passes it to next-themes. No
// other inline script or style can run, and scripts load from this origin only.
export function proxy(request: NextRequest) {
  const nonce = btoa(crypto.randomUUID());
  const csp = contentSecurityPolicy(nonce, process.env.NODE_ENV === "development");

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  // Pages and API responses; not the build's static files or the images, which get a
  // fixed policy in next.config.ts.
  matcher: ["/((?!_next/static|_next/image|img/).*)"],
};
