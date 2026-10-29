// GET /api/linked-role/start (the Linked Roles Verification URL). See src/server/linked-role/.
import { createStart } from "@/server/linked-role/flow";

export const dynamic = "force-dynamic";

const start = createStart();
// Every method but GET and HEAD gets the 405 page with Allow: GET.
const handle = async (req: Request) => start(req);

export const GET = handle;
export const HEAD = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const OPTIONS = handle;
