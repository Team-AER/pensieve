// Native disclosure state changes immediately; motion only carries the paper between measured sizes.
(function () {
  if (window.__pensievePaperMotion) return;
  window.__pensievePaperMotion = true;
  const media = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  const running = new Map();
  function stopAll() { for (const stop of [...running.values()]) stop(); }
  media?.addEventListener('change', (e) => { if (e.matches) stopAll(); });
  document.body.addEventListener('htmx:beforeSwap', stopAll);
  window.addEventListener('pagehide', stopAll);
  document.addEventListener('click', (event) => {
    const head = event.target.closest('.pstory > .pstory-head');
    if (!head || event.target.closest('a, button, input, select, textarea')) return;
    const story = head.parentElement;
    if (media?.matches || typeof story.animate !== 'function') return;
    event.preventDefault();
    const from = story.getBoundingClientRect().height;
    const opening = !story.open;
    let ghost = null;
    if (!opening) {
      const body = story.querySelector('.pstory-body');
      if (body) {
        const rect = body.getBoundingClientRect();
        ghost = body.cloneNode(true);
        ghost.removeAttribute('id');
        ghost.querySelectorAll('[id]').forEach((e) => e.removeAttribute('id'));
        ghost.setAttribute('aria-hidden', 'true');
        ghost.inert = true;
        ghost.classList.add('paper-fold-ghost');
        Object.assign(ghost.style, { position: 'fixed', left: rect.left + 'px', top: rect.top + 'px', width: rect.width + 'px', height: rect.height + 'px' });
      }
    }
    running.get(story)?.();
    story.open = opening;
    const to = story.getBoundingClientRect().height;
    const animations = [];
    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      animations.forEach((a) => a.cancel());
      ghost?.remove();
      story.style.removeProperty('overflow');
      running.delete(story);
    };
    try {
      story.style.overflow = 'clip';
      const duration = Math.min(460, 320 + Math.abs(to - from) * 0.15);
      const fold = story.animate([{ height: from + 'px' }, { height: to + 'px' }], { duration, easing: 'cubic-bezier(.2,.8,.2,1)' });
      animations.push(fold);
      if (ghost) {
        document.body.append(ghost);
        animations.push(ghost.animate([{ opacity: 1, clipPath: 'inset(0)' }, { opacity: 0, clipPath: 'inset(0 0 100% 0)' }], { duration: duration * 0.8, easing: 'ease-in' }));
      } else {
        const body = story.querySelector('.pstory-body');
        if (body) animations.push(body.animate([{ opacity: 0, transform: 'translateY(-12px)' }, { opacity: 1, transform: 'none' }], { duration, easing: 'cubic-bezier(.2,.8,.2,1)' }));
      }
      running.set(story, settle);
      fold.finished.then(settle, settle);
    } catch (_) { settle(); }
  });
})();
