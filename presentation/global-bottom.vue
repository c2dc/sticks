<template>
  <div class="slide-pagination">
    {{ $nav.currentPage }}/{{ $nav.total }}
  </div>
</template>

<style scoped>
.slide-pagination {
  position: absolute;
  right: 0.9rem;
  bottom: 0.6rem;
  z-index: 100;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--muted, #64748b);
  font-variant-numeric: tabular-nums;
  pointer-events: none;
  user-select: none;
}
</style>
