import { Box, Text } from '@hermes/ink'

import type { GridTestState } from '../app/interfaces.js'
import type { Theme } from '../theme.js'

import { WidgetGrid, type WidgetGridWidget } from './widgetGrid.js'

interface GridTestOverlayProps {
  cols: number
  state: GridTestState
  t: Theme
}

const NESTED_GAP = 1
// Heights are odd so a single line is true-centered: border, blank, text, blank, border.
const FLAT_CELL_HEIGHT = 5
const NESTED_CELL_HEIGHT = 9
const MINI_CELL_HEIGHT = 3

// Sparse "every-other" pattern so the nested toggle visibly differs from the active cell.
const showsNestedPreview = (row: number, col: number) => row % 2 === 0 && col % 2 === 0

export function GridTestOverlay({ cols, state, t }: GridTestOverlayProps) {
  const gridCols = Math.max(12, cols)
  const activeIdx = state.activeRow * state.cols + state.activeCol
  const activeLabel = `c${activeIdx + 1}`

  const widgets: WidgetGridWidget[] = Array.from({ length: state.rows * state.cols }, (_, idx) => {
    const row = Math.floor(idx / state.cols)
    const col = idx % state.cols
    const active = idx === activeIdx
    const label = `c${idx + 1}`

    return {
      id: `cell-${idx}`,
      render: width => (
        <GridCell
          active={active}
          label={label}
          nested={state.nested && showsNestedPreview(row, col)}
          nestedMode={state.nested}
          t={t}
          width={width}
        />
      )
    }
  })

  return (
    <Box flexDirection="column" paddingY={1} width={gridCols}>
      <Box justifyContent="space-between" marginBottom={1} width="100%">
        <Text bold color={t.color.primary}>
          {state.zoomed ? `/grid-test / r${state.activeRow + 1} c${state.activeCol + 1}` : '/grid-test'}
        </Text>
        <Text color={t.color.muted}>
          {state.cols}x{state.rows} grid
        </Text>
      </Box>

      <Text color={t.color.muted} wrap="truncate">
        {state.zoomed
          ? 'arrows/hjkl switch cell · Esc/q back · Ctrl+C close'
          : 'arrows/hjkl move · Enter zoom · d dialog · +/- cols · [] rows · g gap · p pad · n nest · q close'}
      </Text>

      <Box marginTop={1}>
        {state.zoomed ? (
          <ZoomedGridCell cols={gridCols} parentLabel={activeLabel} t={t} />
        ) : (
          <WidgetGrid
            cols={gridCols}
            columns={state.cols}
            gap={state.gap ?? (state.nested ? NESTED_GAP : undefined)}
            minColumnWidth={1}
            paddingX={state.paddingX ?? undefined}
            rowGap={0}
            widgets={widgets}
          />
        )}
      </Box>

      {!state.zoomed && (
        <Box marginTop={1}>
          <Text color={t.color.muted} wrap="truncate">
            gap {state.gap ?? 'auto'} · pad {state.paddingX ?? 'auto'} · nested {state.nested ? 'on' : 'off'}
          </Text>
        </Box>
      )}
    </Box>
  )
}

function GridCell({
  active,
  label,
  nested,
  nestedMode,
  t,
  width
}: {
  active: boolean
  label: string
  nested: boolean
  nestedMode: boolean
  t: Theme
  width: number
}) {
  const padX = width >= 14 ? 1 : 0
  const inner = Math.max(1, width - 2 - padX * 2)
  const borderColor = active ? t.color.primary : t.color.border
  const height = nestedMode ? NESTED_CELL_HEIGHT : FLAT_CELL_HEIGHT
  const labelColor = active ? t.color.primary : t.color.label

  return (
    <Box
      borderColor={borderColor}
      borderStyle="round"
      flexDirection="column"
      height={height}
      paddingX={padX}
      width={width}
    >
      {nested && width >= 10 ? (
        <>
          <Box justifyContent="center" width={inner}>
            <Text bold={active} color={labelColor}>
              {label}
            </Text>
          </Box>

          <WidgetGrid
            cols={inner}
            columns={2}
            depth={1}
            gap={NESTED_GAP}
            minColumnWidth={1}
            paddingX={0}
            rowGap={0}
            widgets={childCellWidgets(t, 3, 2)}
          />
        </>
      ) : (
        <Box alignItems="center" flexGrow={1} justifyContent="center" width={inner}>
          <Text bold={active} color={labelColor}>
            {label}
          </Text>
        </Box>
      )}
    </Box>
  )
}

function ZoomedGridCell({ cols, parentLabel, t }: { cols: number; parentLabel: string; t: Theme }) {
  const childColumns = cols >= 72 ? 4 : 2
  const contentWidth = Math.max(1, cols - 4)

  return (
    <Box
      borderColor={t.color.primary}
      borderStyle="round"
      flexDirection="column"
      paddingX={1}
      paddingY={1}
      width={cols}
    >
      <WidgetGrid
        cols={contentWidth}
        columns={2}
        depth={1}
        gap={1}
        minColumnWidth={1}
        paddingX={0}
        rowGap={0}
        widgets={[
          {
            children: (
              <Text bold color={t.color.primary}>
                parent {parentLabel}
              </Text>
            ),
            id: 'header-title'
          },
          {
            children: (
              <Box justifyContent="flex-end" width="100%">
                <Text color={t.color.muted}>nested child grid</Text>
              </Box>
            ),
            id: 'header-meta'
          }
        ]}
      />

      <Box height={1} />

      <WidgetGrid
        cols={contentWidth}
        columns={childColumns}
        depth={1}
        gap={NESTED_GAP}
        minColumnWidth={1}
        paddingX={0}
        rowGap={0}
        widgets={childCellWidgets(t, childColumns * 2, childColumns)}
      />
    </Box>
  )
}

const childCellWidgets = (t: Theme, count: number, columns: number): WidgetGridWidget[] => {
  const colors = [t.color.ok, t.color.warn, t.color.accent, t.color.muted, t.color.primary]
  // 3-cell preview: two side-by-side, third spans the full row.
  const lastSpansRow = count === 3

  return Array.from({ length: count }, (_, idx) => ({
    colSpan: lastSpansRow && idx === count - 1 ? columns : 1,
    id: `child-c${idx + 1}`,
    render: w => <MiniCell color={colors[idx % colors.length]!} label={`c${idx + 1}`} width={w} />
  }))
}

function MiniCell({ color, label, width }: { color: string; label: string; width: number }) {
  return (
    <Box
      alignItems="center"
      borderColor={color}
      borderStyle="single"
      height={MINI_CELL_HEIGHT}
      justifyContent="center"
      overflow="hidden"
      width={width}
    >
      <Text color={color}>{label}</Text>
    </Box>
  )
}
