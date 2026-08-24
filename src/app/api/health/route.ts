import { NextResponse } from "next/server";

export async function GET() {
  return NextResponse.json({ status: "ok", service: "forex-trader-video-intelligence", timestamp: new Date().toISOString() });
}
