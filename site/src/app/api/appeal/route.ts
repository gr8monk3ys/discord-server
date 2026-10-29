import { createHandler, readConfig } from "@/server/appeal";

// Rendered per request, never at build time: the settings are read from the Vercel
// project's environment variables when the function starts.
export const dynamic = "force-dynamic";

let handler: ReturnType<typeof createHandler> | null = null;
function appeal(req: Request): Promise<Response> {
  handler ??= createHandler({ config: readConfig(process.env) });
  return handler(req);
}

// POST is the form; every other method gets the handler's own 405 with Allow: POST.
export const POST = appeal;
export const GET = appeal;
export const HEAD = appeal;
export const PUT = appeal;
export const PATCH = appeal;
export const DELETE = appeal;
export const OPTIONS = appeal;
