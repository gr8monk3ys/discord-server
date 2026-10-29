// GET /api/linked-role/callback (Discord's OAuth2 redirect). See src/server/linked-role/.
import { createCallback } from "@/server/linked-role/flow";

export const dynamic = "force-dynamic";

const callback = createCallback();
const handle = (req: Request) => callback(req);

export const GET = handle;
export const HEAD = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const OPTIONS = handle;
