import { prisma } from "./db";

function isWin(result: string | null) {
  return /win|target|tp|profit/i.test(result ?? "");
}
function isLoss(result: string | null) {
  return /loss|stop|sl|stopped/i.test(result ?? "");
}

export async function aggregateStrategyPatterns() {
  const trades = await prisma.trade.findMany({
    where: { validationStatus: { in: ["VALID", "UNCERTAIN"] } },
    include: { video: { select: { traderId: true } } },
  });
  const groups = new Map<string, typeof trades>();
  for (const trade of trades) {
    if (!trade.setupName) continue;
    const key = `${trade.video.traderId ?? "global"}::${trade.setupName}`;
    const group = groups.get(key) ?? [];
    group.push(trade);
    groups.set(key, group);
  }

  for (const [key, group] of groups) {
    const [traderKey, name] = key.split("::");
    const traderId = traderKey === "global" ? null : traderKey;
    const wins = group.filter((trade) => isWin(trade.result)).length;
    const losses = group.filter((trade) => isLoss(trade.result)).length;
    const rValues = group.map((trade) => trade.riskReward).filter((value): value is number => value !== null);
    const averageR = rValues.length ? rValues.reduce((sum, value) => sum + value, 0) / rValues.length : null;
    const confidence = group.reduce((sum, trade) => sum + trade.confidence, 0) / group.length;
    const existing = await prisma.strategyPattern.findFirst({ where: { traderId, name } });
    const data = {
      traderId,
      name,
      description: `Observed recurring setup across ${group.length} identified trade${group.length === 1 ? "" : "s"}.`,
      conditions: { setupName: name },
      occurrences: group.length,
      wins,
      losses,
      observedFrequency: trades.length ? group.length / trades.length : null,
      averageR,
      confidence,
      sourceTradeCount: group.length,
    };
    const pattern = existing
      ? await prisma.strategyPattern.update({ where: { id: existing.id }, data })
      : await prisma.strategyPattern.create({ data });
    await prisma.patternTrade.deleteMany({ where: { patternId: pattern.id } });
    if (group.length) {
      await prisma.patternTrade.createMany({
        data: group.map((trade) => ({ patternId: pattern.id, tradeId: trade.id })),
        skipDuplicates: true,
      });
    }
  }
  return groups.size;
}

export async function synthesizePlaybooks() {
  const traders = await prisma.trader.findMany({
    include: {
      videos: { include: { trades: true } },
      patterns: { orderBy: { occurrences: "desc" } },
    },
  });
  const targets = traders.length ? traders : [{ id: "global", displayName: "Observed Trader", videos: [], patterns: [] }];
  for (const trader of targets) {
    const trades = trader.videos.flatMap((video) => video.trades);
    const symbols = [...new Set(trades.map((trade) => trade.symbol).filter(Boolean))];
    const timeframes = [...new Set(trades.flatMap((trade) => Array.isArray(trade.timeframes) ? trade.timeframes.map(String) : []))];
    const valid = trades.filter((trade) => trade.validationStatus === "VALID");
    const wins = valid.filter((trade) => isWin(trade.result)).length;
    const losses = valid.filter((trade) => isLoss(trade.result)).length;
    const content = [
      `# Trader Profile\n${trader.displayName}`,
      `## Markets Traded\n${symbols.length ? symbols.join(", ") : "Unknown from current evidence"}`,
      `## Primary Timeframes\n${timeframes.length ? timeframes.join(", ") : "Unknown from current evidence"}`,
      `## Entry Models\n${trader.patterns.length ? trader.patterns.map((pattern) => `- ${pattern.name}: ${pattern.occurrences} observed occurrences`).join("\n") : "No recurring entry model has enough evidence yet."}`,
      `## Trade Management\nDerived only from validated/uncertain analyzed-video trades; unsupported management rules remain unknown.`,
      `## Evidence Statistics\nIdentified trades: ${trades.length}; validated trades: ${valid.length}; observed wins: ${wins}; observed losses: ${losses}. These are not audited-history statistics.`,
      `## Contradictions / Unclear Rules\nSee contradiction records linked to timestamped source claims.`,
    ].join("\n\n");
    const sourceId = trader.id;
    const existing = await prisma.knowledgeChunk.findFirst({ where: { sourceType: "PLAYBOOK", sourceId } });
    if (existing) await prisma.knowledgeChunk.update({ where: { id: existing.id }, data: { title: `${trader.displayName} observed playbook`, content } });
    else await prisma.knowledgeChunk.create({ data: { sourceType: "PLAYBOOK", sourceId, title: `${trader.displayName} observed playbook`, content } });
  }
}

const contradictionTopics = ["confirmation", "breakeven", "break even", "stop loss", "target", "liquidity", "retest"];
export async function detectContradictions() {
  const traders = await prisma.trader.findMany({ include: { videos: { include: { transcripts: true } } } });
  let created = 0;
  for (const trader of traders) {
    await prisma.contradiction.deleteMany({ where: { traderId: trader.id } });
    const segments = trader.videos.flatMap((video) => video.transcripts.map((segment) => ({ video, segment })));
    for (const topic of contradictionTopics) {
      const positive = segments.find(({ segment }) => segment.text.toLowerCase().includes(topic) && /always|must|only|wait/i.test(segment.text));
      const negative = segments.find(({ segment }) => segment.text.toLowerCase().includes(topic) && /never|don't|do not|without|no need/i.test(segment.text));
      if (!positive || !negative) continue;
      await prisma.contradiction.create({
        data: {
          traderId: trader.id,
          statementA: positive.segment.text,
          statementB: negative.segment.text,
          sourceA: { videoId: positive.video.id, youtubeVideoId: positive.video.youtubeVideoId, timestamp: positive.segment.startSeconds },
          sourceB: { videoId: negative.video.id, youtubeVideoId: negative.video.youtubeVideoId, timestamp: negative.segment.startSeconds },
          evidenceLevel: "DIRECT",
          confidence: 0.85,
          status: "POTENTIAL",
        },
      });
      created += 1;
    }
  }
  return created;
}
