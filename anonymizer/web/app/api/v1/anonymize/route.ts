import { POST as submit, GET as poll, DELETE as cancel } from "@/app/api/anonymize/route";
import { apiKeyOnly } from "@/lib/public-api";

export const runtime = "nodejs";
export const maxDuration = 280;
export const POST = apiKeyOnly(submit);
export const GET = apiKeyOnly(poll);
export const DELETE = apiKeyOnly(cancel);
