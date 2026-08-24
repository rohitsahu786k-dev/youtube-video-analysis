import { NextResponse } from "next/server";
import { storageProvider } from "@/lib/storage";

export const runtime = "nodejs";

export async function GET(_req: Request, { params }: { params: Promise<{ key: string[] }> }) {
  try {
    const { key } = await params;
    const storage = storageProvider();
    const bytes = await storage.read(key.join("/"));
    const filename = key.at(-1) ?? "evidence";
    const contentType = filename.endsWith(".webp") ? "image/webp" : filename.endsWith(".jpg") || filename.endsWith(".jpeg") ? "image/jpeg" : "application/octet-stream";
    return new NextResponse(new Uint8Array(bytes), {
      headers: {
        "content-type": contentType,
        "cache-control": "private, max-age=3600",
        "x-content-type-options": "nosniff",
      },
    });
  } catch {
    return NextResponse.json({ error: "Evidence file not found" }, { status: 404 });
  }
}
