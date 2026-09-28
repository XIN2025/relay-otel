import { NextResponse } from "next/server";

import { getRuntimeHealth } from "@/lib/runtime-health";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  return NextResponse.json(await getRuntimeHealth(), {
    headers: { "cache-control": "no-store" },
  });
}
