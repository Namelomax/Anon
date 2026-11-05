import { handleLeadRequest } from "@/lib/leads";

export const runtime = "nodejs";
export const maxDuration = 45;

export async function POST(request: Request) {
  return handleLeadRequest(request);
}
