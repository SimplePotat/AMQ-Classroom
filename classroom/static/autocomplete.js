'use strict';

// Search the downloaded title catalog locally: typing never contacts MAL or the
// classroom server. Options are text nodes, including provider-supplied names.
class AnimeAutocomplete {
  constructor() {
    this.entries = [];
    this.active = -1;
    this.limit = 100;
  }

  key(value) {
    return value.normalize('NFKD').toLowerCase().replace(/ß/g, 'ss').replace(/[^\p{L}\p{N}]/gu, '');
  }

  setTitles(titles) {
    const catalogKey = JSON.stringify(titles);
    if (this.catalogKey === catalogKey) return;
    this.catalogKey = catalogKey;
    const seen = new Set();
    this.entries = [];
    for (const title of titles) {
      if (typeof title !== 'string' || !title.trim() || [...title].length > 200) continue;
      const text = title.trim(), identity = text.toLowerCase(), key = this.key(text);
      if (!key || seen.has(identity)) continue;
      seen.add(identity);
      this.entries.push({text, key});
    }
    this.entries.sort((a, b) => a.text.localeCompare(b.text));
    if (this.input && !this.popup.hidden) this.search();
  }

  bind(input) {
    this.close();
    this.events?.abort();
    this.input = input;
    if (!input) return;
    this.popup = document.querySelector('#guess-suggestions');
    this.list = document.querySelector('#guess-options');
    this.status = document.querySelector('#guess-suggestion-status');
    this.composing = false;
    this.events = new AbortController();
    const options = {signal:this.events.signal};
    input.addEventListener('input', () => { if (!this.composing) this.search(); }, options);
    input.addEventListener('focus', () => this.search(), options);
    input.addEventListener('click', () => { if (this.popup.hidden) this.search(); }, options);
    input.addEventListener('compositionstart', () => { this.composing = true; this.close(); }, options);
    input.addEventListener('compositionend', () => { this.composing = false; this.search(); }, options);
    input.addEventListener('keydown', event => this.keydown(event), options);
    this.list.addEventListener('pointerdown', event => {
      // Keep the caret on desktop. Touch retains native scrolling and tapping.
      if (event.pointerType === 'mouse') event.preventDefault();
    }, options);
    this.list.addEventListener('click', event => {
      const option = event.target.closest('[role="option"]');
      if (option) this.choose(Number(option.dataset.index));
    }, options);
    document.addEventListener('pointerdown', event => {
      if (!event.target.closest('.guess-autocomplete')) this.close();
    }, options);
    document.addEventListener('focusin', event => {
      if (!event.target.closest('.guess-autocomplete')) this.close();
    }, options);
    document.addEventListener('scroll', () => this.place(), {...options,capture:true});
    window.addEventListener('resize', () => this.place(), options);
    window.addEventListener('blur', () => this.close(), options);
    window.visualViewport?.addEventListener('resize', () => this.place(), options);
    window.visualViewport?.addEventListener('scroll', () => this.place(), options);
  }

  search() {
    if (!this.input || this.input.disabled || this.composing) return this.close();
    const query = this.key(this.input.value);
    if (!query) return this.close();
    const words = this.input.value.trim().split(/\s+/u).map(word => this.key(word)).filter(Boolean);
    const groups = [[], [], []];
    let total = 0;
    for (const entry of this.entries) {
      if (!words.every(word => entry.key.includes(word))) continue;
      total++;
      const rank = entry.key === query ? 0 : entry.key.startsWith(query) ? 1 : 2;
      if (groups[rank].length < this.limit) groups[rank].push(entry.text);
    }
    this.matches = groups.flat().slice(0, this.limit);
    this.active = -1;
    this.input.removeAttribute('aria-activedescendant');
    const fragment = document.createDocumentFragment();
    this.matches.forEach((text, index) => {
      const option = document.createElement('li');
      option.id = `guess-option-${index}`;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', 'false');
      option.dataset.index = index;
      option.textContent = text;
      fragment.append(option);
    });
    this.list.replaceChildren(fragment);
    this.list.scrollTop = 0;
    this.status.textContent = !total ? 'No matching titles. You can still type your answer.' :
      total > this.limit ? `First ${this.limit} matches · keep typing to narrow the list` :
      `${total} ${total === 1 ? 'match' : 'matches'} · select a title, then send your answer`;
    this.popup.hidden = false;
    this.input.setAttribute('aria-expanded', 'true');
    this.place();
  }

  place() {
    if (!this.input || this.popup.hidden) return;
    const rect = this.input.getBoundingClientRect(), viewport = window.visualViewport;
    const top = viewport?.offsetTop || 0, bottom = top + (viewport?.height || innerHeight);
    const below = bottom - rect.bottom - 10, above = rect.top - top - 10;
    const upwards = below < Math.min(180, above);
    const room = upwards ? above : below;
    if (room < 60 || rect.bottom < top || rect.top > bottom) return this.close();
    Object.assign(this.popup.style, {
      left:`${rect.left}px`, width:`${rect.width}px`, maxHeight:`${Math.min(320, room)}px`,
      top:`${upwards ? rect.top - 6 : rect.bottom + 6}px`,
      transform:upwards ? 'translateY(-100%)' : 'none',
    });
  }

  keydown(event) {
    // Enter during Japanese/Chinese IME composition must not choose or submit.
    if (event.isComposing || this.composing || event.keyCode === 229) return;
    if (event.key === 'Escape') {
      if (!this.popup.hidden) event.preventDefault();
      return this.close();
    }
    if (event.key === 'Tab') return this.close();
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      if (this.popup.hidden) this.search();
      if (!this.matches?.length || this.popup.hidden) return;
      event.preventDefault();
      this.active = event.key === 'ArrowDown' ? (this.active + 1) % this.matches.length :
        (this.active <= 0 ? this.matches.length : this.active) - 1;
      for (const [index, option] of [...this.list.children].entries()) {
        option.setAttribute('aria-selected', String(index === this.active));
      }
      const option = this.list.children[this.active];
      this.input.setAttribute('aria-activedescendant', option.id);
      // Scroll only the list; moving the page here hides the mobile keyboard.
      if (option.offsetTop < this.list.scrollTop) this.list.scrollTop = option.offsetTop;
      else if (option.offsetTop + option.offsetHeight > this.list.scrollTop + this.list.clientHeight)
        this.list.scrollTop = option.offsetTop + option.offsetHeight - this.list.clientHeight;
    } else if (event.key === 'Enter' && !this.popup.hidden && this.active >= 0) {
      event.preventDefault();
      this.choose(this.active);
    }
  }

  choose(index) {
    if (!this.input || this.input.disabled || this.popup.hidden || !this.matches?.[index]) return;
    this.input.value = this.matches[index];
    this.input.dispatchEvent(new Event('input', {bubbles:true}));
    this.input.focus({preventScroll:true});
    this.close();
  }

  close() {
    if (this.popup) this.popup.hidden = true;
    if (this.status) this.status.textContent = '';
    this.input?.setAttribute('aria-expanded', 'false');
    this.input?.removeAttribute('aria-activedescendant');
    this.active = -1;
    this.matches = [];
  }
}
