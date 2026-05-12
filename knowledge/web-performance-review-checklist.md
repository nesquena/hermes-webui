# Web Performance Review Checklist

Purpose: compact PR/deploy gate for delivered web work, especially AI-generated or AI-assisted code.

Source basis:
- Core Web Vitals: LCP, INP, CLS
- Addy Osmani, `Web Performance Engineering in the Age of AI` — verified local PDF TOC + OpenKB brief
- OpenKB concepts: user-centric performance, AI-generated code performance debt, third-party script governance

## 0. Verdict

- [ ] PASS
- [ ] PASS WITH RISKS
- [ ] FAIL
- [ ] BLOCK

## 1. Evidence Required

- [ ] Production-like build tested, not dev mode
- [ ] Mobile or throttled mobile profile tested
- [ ] Route/page tested is listed
- [ ] Tool output attached: Lighthouse, PageSpeed, WebPageTest, DevTools, or RUM
- [ ] Field/RUM data used when available; Lighthouse is not treated as truth alone

## 2. Core Metrics

- [ ] LCP p75 <= 2.5s
- [ ] INP p75 <= 200ms
- [ ] CLS p75 <= 0.1
- [ ] TTFB acceptable for route
- [ ] Lighthouse mobile meets agreed target

## 3. Loading / LCP

- [ ] LCP element identified
- [ ] Hero/LCP image or content is not lazy-loaded
- [ ] Critical resource priority is intentional: preload/fetchpriority only when justified
- [ ] Render-blocking CSS/JS minimized
- [ ] No avoidable API/data waterfall before main content

## 4. Responsiveness / INP

- [ ] Critical interactions have no obvious long tasks
- [ ] Click/input/submit/menu interactions feel responsive
- [ ] Hydration/client JS does not block interaction too long
- [ ] Heavy work is deferred, split, memoized, or moved off main thread

## 5. Visual Stability / CLS

- [ ] Images/video/iframes reserve dimensions
- [ ] Fonts do not cause major layout shift
- [ ] Skeleton/loading UI matches final layout size
- [ ] Dynamic content, ads, embeds, modals, and toasts do not shift layout unexpectedly

## 6. AI-Generated Code Debt

- [ ] Generated code is reviewed for bundle bloat and unnecessary dependencies
- [ ] Generated components do not create excessive hydration/client-side JS cost
- [ ] Semantic HTML and accessibility are checked, not only visual output
- [ ] AI-added images, fonts, libraries, and third-party scripts follow this checklist
- [ ] Performance debt introduced by AI output is fixed or has an owner before merge

## 7. JavaScript

- [ ] Initial JS is within budget
- [ ] Code splitting is used where useful
- [ ] No large new dependency without justification
- [ ] Unused JS is removed or acceptable
- [ ] Expensive client components are justified

## 8. CSS / Rendering

- [ ] CSS is minified
- [ ] Unused CSS is low or acceptable
- [ ] Animations use transform/opacity where possible
- [ ] No obvious forced reflow/layout thrashing
- [ ] DOM size is not excessive

## 9. Images / Media / Fonts

- [ ] Images are compressed and modern format where appropriate
- [ ] Responsive images use srcset/sizes when needed
- [ ] Below-the-fold images are lazy-loaded
- [ ] Font families/weights are minimal
- [ ] Critical fonts are handled intentionally: preload/subset/font-display as appropriate

## 10. Third-Party Governance

- [ ] Every third-party script has owner and business reason
- [ ] Third-party scripts do not block critical rendering
- [ ] Loading strategy is explicit: defer, async, lazy, consent-gated, or after interaction
- [ ] Third-party impact on bundle, main thread, and INP is checked
- [ ] New third-party dependency has rollback/removal path

## 11. Network / Cache

- [ ] Static assets use hashed filenames
- [ ] Immutable assets have long cache headers
- [ ] Brotli/gzip enabled
- [ ] CDN used where appropriate
- [ ] Request count and transfer size are reasonable
- [ ] Preload/preconnect used only for truly critical resources

## 12. Backend / API

- [ ] Slow APIs do not block first meaningful content unnecessarily
- [ ] API calls are not waterfalling when they can be parallelized or cached
- [ ] Server/database latency is within budget
- [ ] Loading and error states are present

## 13. Monitoring / Regression

- [ ] Bundle size diff checked for PR
- [ ] Performance budget documented or agreed
- [ ] RUM/web-vitals monitoring exists or is planned for important pages
- [ ] New performance debt has owner and follow-up
- [ ] Performance regression gate exists for important routes

## 14. Blockers

Block merge/deploy if any apply:

- [ ] LCP > 4s on important public route
- [ ] INP > 500ms on critical interaction
- [ ] CLS > 0.25
- [ ] Hero/LCP image is unoptimized or lazy-loaded
- [ ] Massive new dependency without justification
- [ ] Third-party script blocks render and has no owner
- [ ] AI-generated UI ships without performance review
- [ ] No production-like performance evidence

## 15. Review Summary Template

Verdict:

Evidence:
- Route:
- Build/env:
- Device/network:
- Tool:

Metrics:
- LCP:
- INP:
- CLS:
- Lighthouse mobile:
- TTFB:
- Initial JS:
- Transfer size:
- Requests:

Must fix:
- 

Should fix:
- 

Accepted tradeoffs:
- 

Unknown / not verified:
- 
