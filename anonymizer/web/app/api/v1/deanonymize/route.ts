import { POST as restore } from "@/app/api/deanonymize/route";
import { apiKeyOnly } from "@/lib/public-api";

export const runtime = "nodejs";
export const maxDuration = 120;
export const POST = apiKeyOnly(restore);
