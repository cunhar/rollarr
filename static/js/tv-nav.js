/**
 * static/js/tv-nav.js
 * -------------------
 * Spatial D-Pad Navigation engine for TV remotes.
 */

(function initTVNavigation() {
    const FOCUSABLE_SELECTOR = 'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), details summary, [tabindex="0"]';

    function getFocusableElements() {
        return Array.from(document.querySelectorAll(FOCUSABLE_SELECTOR)).filter(el => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        });
    }

    function getCenter(rect) {
        return {
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2
        };
    }

    function findNearestElement(currentEl, direction) {
        const candidates = getFocusableElements().filter(el => el !== currentEl);
        if (candidates.length === 0) return null;

        const currentRect = currentEl.getBoundingClientRect();
        const currentCenter = getCenter(currentRect);

        let bestCandidate = null;
        let minDistance = Infinity;

        candidates.forEach(cand => {
            const candRect = cand.getBoundingClientRect();
            const candCenter = getCenter(candRect);

            let dx = candCenter.x - currentCenter.x;
            let dy = candCenter.y - currentCenter.y;

            let isDirectionValid = false;

            switch (direction) {
                case 'ArrowUp':
                    isDirectionValid = dy < -2;
                    break;
                case 'ArrowDown':
                    isDirectionValid = dy > 2;
                    break;
                case 'ArrowLeft':
                    isDirectionValid = dx < -2;
                    break;
                case 'ArrowRight':
                    isDirectionValid = dx > 2;
                    break;
            }

            if (!isDirectionValid) return;

            // Spatial weighting: penalize off-axis distance so aligned elements win
            let primaryDist = (direction === 'ArrowUp' || direction === 'ArrowDown') ? Math.abs(dy) : Math.abs(dx);
            let secondaryDist = (direction === 'ArrowUp' || direction === 'ArrowDown') ? Math.abs(dx) : Math.abs(dy);
            
            let score = primaryDist + (secondaryDist * 2.5);

            if (score < minDistance) {
                minDistance = score;
                bestCandidate = cand;
            }
        });

        return bestCandidate;
    }

    document.addEventListener('keydown', (e) => {
        const navKeys = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'];
        if (!navKeys.includes(e.key)) return;

        const active = document.activeElement;
        const focusables = getFocusableElements();

        if (focusables.length === 0) return;

        // If typing inside an input/select and pressing Left/Right, allow native cursor movement unless at boundary
        if (active && (active.tagName === 'INPUT' || active.tagName === 'SELECT')) {
            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                return;
            }
        }

        // If no element is currently focused or body is focused, pick active tab or first visible element
        if (!active || active === document.body || !focusables.includes(active)) {
            e.preventDefault();
            const firstTarget = document.querySelector('.tab-btn.active') || focusables[0];
            if (firstTarget) {
                firstTarget.focus();
                firstTarget.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
            return;
        }

        const nextEl = findNearestElement(active, e.key);
        if (nextEl) {
            e.preventDefault();
            nextEl.focus();
            nextEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    });
})();
