/**
 * GitLab Duo Chat Network Request Capturer
 * Inject this into gitlab.com page to intercept all AI/chat related API calls
 */
async function run(args) {
  window.__gitlabApiCaptures = window.__gitlabApiCaptures || [];
  
  const captures = window.__gitlabApiCaptures;
  const startTime = Date.now();
  
  // Intercept fetch
  const origFetch = window.fetch;
  window.fetch = async function(...fetchArgs) {
    const [resource, options] = fetchArgs;
    const url = typeof resource === 'string' ? resource : (resource.url || resource.toString());
    
    // Capture all requests - we'll filter later
    const capture = {
      id: captures.length,
      url: url,
      method: options?.method || 'GET',
      requestHeaders: {},
      requestBody: null,
      timestamp: new Date().toISOString(),
      elapsedMs: Date.now() - startTime,
      responseStatus: null,
      responseHeaders: {},
      responseBodyPreview: null,
      responseContentType: null,
      isAiRelated: false
    };
    
    // Extract request headers
    if (options?.headers) {
      if (options.headers instanceof Headers) {
        options.headers.forEach((v, k) => capture.requestHeaders[k] = v);
      } else if (typeof options.headers === 'object') {
        capture.requestHeaders = {...options.headers};
      }
    }
    
    // Capture body for POST/PUT/PATCH
    if (options?.body && ['POST', 'PUT', 'PATCH'].includes(capture.method.toUpperCase())) {
      try {
        if (typeof options.body === 'string') {
          capture.requestBody = options.body.substring(0, 10000);
        } else if (options.body instanceof FormData) {
          capture.requestBody = '[FormData] ' + Array.from(options.body.entries()).map(([k,v]) => `${k}=${v}`).join('&');
        } else {
          capture.requestBody = String(options.body).substring(0, 10000);
        }
      } catch(e) {
        capture.requestBody = '[Could not read body]';
      }
    }
    
    // Check if AI-related
    const aiKeywords = ['ai', 'chat', 'duo', 'agent', 'graphql', 'llm', 'completion', 'stream'];
    const lowerUrl = url.toLowerCase();
    capture.isAiRelated = aiKeywords.some(k => lowerUrl.includes(k));
    
    // Always capture GraphQL and /api/ requests
    if (lowerUrl.includes('graphql') || lowerUrl.includes('/api/')) {
      capture.isAiRelated = true;
    }
    
    const t0 = performance.now();
    
    try {
      const response = await origFetch.apply(this, fetchArgs);
      const clone = response.clone();
      
      capture.responseStatus = response.status;
      capture.responseContentType = response.headers.get('content-type') || '';
      
      try {
        const ct = capture.responseContentType.toLowerCase();
        if (ct.includes('json') || ct.includes('text') || ct.includes('event-stream')) {
          const text = await clone.text();
          capture.responseBodyPreview = text.substring(0, 15000);
          
          // For streaming responses, show first few chunks
          if (ct.includes('event-stream')) {
            const lines = text.split('\n').filter(l => l.trim());
            capture.streamEventCount = lines.length;
            capture.firstEvents = lines.slice(0, 10);
          }
        }
      } catch(e) {
        capture.readError = e.message;
      }
      
      capture.elapsedMs = Math.round(performance.now() - t0);
      
      if (capture.isAiRelated) {
        captures.push(capture);
        console.log(`[GITLAB-CAPTURE #${capture.id}] ${capture.method} ${capture.url} → ${capture.responseStatus} (${capture.elapsedMs}ms)`);
        
        if (capture.requestBody) {
          console.log(`[GITLAB-CAPTURE #${capture.id}] Request Body:`, capture.requestBody.substring(0, 2000));
        }
        if (capture.responseBodyPreview) {
          console.log(`[GITLAB-CAPTURE #${capture.id}] Response Preview:`, capture.responseBodyPreview.substring(0, 2000));
        }
      }
      
      return response;
    } catch(err) {
      capture.error = err.message;
      capture.elapsedMs = Math.round(performance.now() - t0);
      if (capture.isAiRelated) {
        captures.push(capture);
      }
      throw err;
    }
  };
  
  // Also intercept XMLHttpRequest
  const origXHROpen = XMLHttpRequest.prototype.open;
  const origXHRSend = XMLHttpRequest.prototype.send;
  const origXHRSetHeader = XMLHttpRequest.prototype.setRequestHeader;
  
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this._glCapture = { method, url, headers: {}, timestamp: new Date().toISOString() };
    return origXHROpen.apply(this, [method, url, ...rest]);
  };
  
  XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
    if (this._glCapture) this._glCapture.headers[name] = value;
    return origXHRSetHeader.apply(this, arguments);
  };
  
  XMLHttpRequest.prototype.send = function(body) {
    if (this._glCapture) {
      const info = this._glCapture;
      info.body = body ? String(body).substring(0, 10000) : null;
      
      const self = this;
      this.addEventListener('load', function() {
        info.responseStatus = self.status;
        info.responseBodyPreview = self.responseText ? self.responseText.substring(0, 15000) : null;
        info.responseContentType = self.getResponseHeader('content-type') || '';
        
        const lowerUrl = info.url.toLowerCase();
        const aiKw = ['ai', 'chat', 'duo', 'agent', 'graphql', 'llm'];
        info.isAiRelated = aiKw.some(k => lowerUrl.includes(k)) || lowerUrl.includes('/api/');
        
        if (info.isAiRelated) {
          info.id = captures.length;
          captures.push(info);
          console.log(`[GITLAB-XHR-CAPTURE #${info.id}] ${info.method} ${info.url} → ${info.responseStatus}`);
        }
      });
    }
    return origXHRSend.apply(this, arguments);
  };
  
  return {
    ok: true,
    summary: "Network interceptor installed. All AI/GraphQL/API requests will be captured.",
    data: {
      message: "Interceptor active. Send a chat message in Duo Chat to capture API details.",
      exportCommand: "copy(JSON.stringify(window.__gitlabApiCaptures, null, 2))",
      viewCommand: "window.__gitlabApiCaptures"
    },
    warnings: []
  };
}
