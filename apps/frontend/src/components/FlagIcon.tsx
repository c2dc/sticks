/**
 * Ícones de bandeira em SVG (vetor), usados no seletor de idioma.
 *
 * Por que não usar emoji (🇧🇷 / 🇺🇸)? Os emojis de bandeira são formados por
 * "regional indicator symbols" e dependem de uma fonte de emoji que saiba
 * combiná-los. O Windows não inclui essa fonte para bandeiras, então o sistema
 * renderiza apenas as letras ("BR", "US") em vez da bandeira. SVG resolve isso
 * de forma consistente em qualquer sistema operacional e navegador.
 *
 * Estes SVGs são desenhos vetoriais das bandeiras reais (proporção 3:2),
 * simplificados o suficiente para ficarem nítidos em tamanho de ícone.
 */

type FlagProps = {
  className?: string
  title?: string
}

/** Bandeira do Brasil (verde, losango amarelo, círculo azul). */
export function FlagBR({ className, title }: FlagProps) {
  return (
    <svg
      viewBox="0 0 30 21"
      className={className}
      role="img"
      aria-label={title ?? "Brasil"}
    >
      {title ? <title>{title}</title> : null}
      <rect width="30" height="21" fill="#009b3a" />
      <path d="M15 2.5 27.5 10.5 15 18.5 2.5 10.5Z" fill="#fedf00" />
      <circle cx="15" cy="10.5" r="4.2" fill="#002776" />
      <path
        d="M11.1 9.2a8 8 0 0 1 7.9 1.2"
        fill="none"
        stroke="#fff"
        strokeWidth="0.9"
      />
    </svg>
  )
}

/** Bandeira dos Estados Unidos (13 listras, cantão azul com estrelas). */
export function FlagUS({ className, title }: FlagProps) {
  const stripes = Array.from({ length: 13 }, (_, i) => (
    <rect
      key={i}
      y={(i * 21) / 13}
      width="30"
      height={21 / 13}
      fill={i % 2 === 0 ? "#b22234" : "#fff"}
    />
  ))
  return (
    <svg
      viewBox="0 0 30 21"
      className={className}
      role="img"
      aria-label={title ?? "Estados Unidos"}
    >
      {title ? <title>{title}</title> : null}
      {stripes}
      <rect width="12" height={(21 * 7) / 13} fill="#3c3b6e" />
      <g fill="#fff">
        {Array.from({ length: 5 }, (_, row) =>
          Array.from({ length: row % 2 === 0 ? 6 : 5 }, (_, col) => {
            const x = row % 2 === 0 ? 1.1 + col * 1.9 : 2.05 + col * 1.9
            const y = 1.1 + row * 2.05
            return <circle key={`${row}-${col}`} cx={x} cy={y} r="0.55" />
          }),
        )}
      </g>
    </svg>
  )
}
