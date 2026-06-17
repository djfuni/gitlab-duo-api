/**
 * Read captured network requests from the interceptor
 */
async function run(args) {
  const captures = window.__gitlabApiCaptures || [];
  
  return {
    ok: true,
    summary: `Found ${captures.length} captured requests`,
    data: {
      total_captures: captures.length,
      captures: captures.map(c => ({
        id: c.id,
        url: c.url,
        method: c.method,
        status: c.responseStatus,
        contentType: c.responseContentType,
        hasRequestBody: !!c.requestBody,
        requestBodyPreview: (c.requestBody || "").substring(0, 3000),
        responseBodyPreview: (c.responseBodyPreview || "").substring(0, 5000),
        requestHeaders: c.requestHeaders || {},
        streamEventCount: c.streamEventCount,
        firstEvents: c.firstEvents ? c.firstEvents.slice(0, 5) : null,
        elapsedMs: c.elapsedMs,
      })),
    },
    warnings: [],
  };
}
