// Only render backend-masked values; never derive previews from raw credentials.
export function secretPreview(value: unknown): string {
  if (typeof value !== 'string') return '***'
  return value.split('\n').map(part => {
    if (part === '***') return part
    return /^[^\s…]{1,5}…[^\s…]{1,3}$/u.test(part) ? part : '***'
  }).join('\n')
}
