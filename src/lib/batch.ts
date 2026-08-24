import { createReadStream } from "node:fs";
import OpenAI from "openai";
import { env } from "./config";

export type BatchStatus = {
  id: string;
  status: string;
  outputFileId: string | null;
  errorFileId: string | null;
};

export class BatchAnalysisService {
  private client() {
    if (!env.OPENAI_API_KEY) throw new Error("OPENAI_API_KEY required");
    return new OpenAI({ apiKey: env.OPENAI_API_KEY });
  }

  isBatchMode() {
    return env.AI_PROCESSING_MODE === "batch";
  }

  async submitJsonl(filePath: string): Promise<BatchStatus> {
    if (!this.isBatchMode()) throw new Error("AI_PROCESSING_MODE must be batch");
    const client = this.client();
    const file = await client.files.create({ file: createReadStream(filePath), purpose: "batch" });
    const batch = await client.batches.create({
      input_file_id: file.id,
      endpoint: "/v1/responses",
      completion_window: "24h",
      metadata: { application: "forex-trader-video-intelligence" },
    });
    return {
      id: batch.id,
      status: batch.status,
      outputFileId: batch.output_file_id ?? null,
      errorFileId: batch.error_file_id ?? null,
    };
  }

  async status(batchId: string): Promise<BatchStatus> {
    const batch = await this.client().batches.retrieve(batchId);
    return {
      id: batch.id,
      status: batch.status,
      outputFileId: batch.output_file_id ?? null,
      errorFileId: batch.error_file_id ?? null,
    };
  }

  async cancel(batchId: string): Promise<BatchStatus> {
    const batch = await this.client().batches.cancel(batchId);
    return {
      id: batch.id,
      status: batch.status,
      outputFileId: batch.output_file_id ?? null,
      errorFileId: batch.error_file_id ?? null,
    };
  }
}
