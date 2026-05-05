import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'

import { useGateway } from '../app/gatewayContext.js'
import type { AppOverlaysProps } from '../app/interfaces.js'
import { $overlayState, patchOverlayState } from '../app/overlayStore.js'
import { $uiSessionId, $uiTheme } from '../app/uiStore.js'

import { FloatBox } from './appChrome.js'
import { MaskedPrompt } from './maskedPrompt.js'
import { ModelPicker } from './modelPicker.js'
import { OverlayHint } from './overlayControls.js'
import { ApprovalPrompt, ClarifyPrompt, ConfirmPrompt } from './prompts.js'
import { SessionPicker } from './sessionPicker.js'
import { SkillsHub } from './skillsHub.js'
import { WidgetGrid, type WidgetGridWidget } from './widgetGrid.js'

const COMPLETION_WINDOW = 16

export function PromptZone({
  cols,
  onApprovalChoice,
  onClarifyAnswer,
  onSecretSubmit,
  onSudoSubmit
}: Pick<AppOverlaysProps, 'cols' | 'onApprovalChoice' | 'onClarifyAnswer' | 'onSecretSubmit' | 'onSudoSubmit'>) {
  const overlay = useStore($overlayState)
  const theme = useStore($uiTheme)

  if (overlay.approval) {
    return (
      <Box flexDirection="column" flexShrink={0} paddingX={1} paddingY={1}>
        <ApprovalPrompt onChoice={onApprovalChoice} req={overlay.approval} t={theme} />
      </Box>
    )
  }

  if (overlay.confirm) {
    const req = overlay.confirm

    const onConfirm = () => {
      patchOverlayState({ confirm: null })
      req.onConfirm()
    }

    const onCancel = () => patchOverlayState({ confirm: null })

    return (
      <Box flexDirection="column" flexShrink={0} paddingX={1} paddingY={1}>
        <ConfirmPrompt onCancel={onCancel} onConfirm={onConfirm} req={req} t={theme} />
      </Box>
    )
  }

  if (overlay.clarify) {
    return (
      <Box flexDirection="column" flexShrink={0} paddingX={1} paddingY={1}>
        <ClarifyPrompt
          cols={cols}
          onAnswer={onClarifyAnswer}
          onCancel={() => onClarifyAnswer('')}
          req={overlay.clarify}
          t={theme}
        />
      </Box>
    )
  }

  if (overlay.sudo) {
    return (
      <Box flexDirection="column" flexShrink={0} paddingX={1} paddingY={1}>
        <MaskedPrompt cols={cols} icon="🔐" label="sudo password required" onSubmit={onSudoSubmit} t={theme} />
      </Box>
    )
  }

  if (overlay.secret) {
    return (
      <Box flexDirection="column" flexShrink={0} paddingX={1} paddingY={1}>
        <MaskedPrompt
          cols={cols}
          icon="🔑"
          label={overlay.secret.prompt}
          onSubmit={onSecretSubmit}
          sub={`for ${overlay.secret.envVar}`}
          t={theme}
        />
      </Box>
    )
  }

  return null
}

export function FloatingOverlays({
  cols,
  compIdx,
  completions,
  onModelSelect,
  onPickerSelect,
  pagerPageSize
}: Pick<AppOverlaysProps, 'cols' | 'compIdx' | 'completions' | 'onModelSelect' | 'onPickerSelect' | 'pagerPageSize'>) {
  const { gw } = useGateway()
  const overlay = useStore($overlayState)
  const sid = useStore($uiSessionId)
  const theme = useStore($uiTheme)

  const hasAny = overlay.modelPicker || overlay.pager || overlay.picker || overlay.skillsHub || completions.length

  if (!hasAny) {
    return null
  }

  const gridCols = Math.max(24, cols - 2)
  const gridMaxColumns = cols >= 120 ? 2 : 1
  const fullSpan = gridMaxColumns
  const capWidth = (cellWidth: number) => Math.max(24, cellWidth - 4)

  // Fixed viewport centered on compIdx — previously the slice end was
  // compIdx + 8 so the dropdown grew from 8 rows to 16 as the user scrolled
  // down, bouncing the height on every keystroke.
  const viewportSize = Math.min(COMPLETION_WINDOW, completions.length)

  const start = Math.max(0, Math.min(compIdx - Math.floor(COMPLETION_WINDOW / 2), completions.length - viewportSize))

  const widgets: WidgetGridWidget[] = []

  if (overlay.picker) {
    widgets.push({
      id: 'picker',
      render: width => (
        <FloatBox color={theme.color.border}>
          <SessionPicker
            gw={gw}
            maxWidth={capWidth(width)}
            onCancel={() => patchOverlayState({ picker: false })}
            onSelect={onPickerSelect}
            t={theme}
          />
        </FloatBox>
      )
    })
  }

  if (overlay.modelPicker) {
    widgets.push({
      id: 'model-picker',
      render: width => (
        <FloatBox color={theme.color.border}>
          <ModelPicker
            gw={gw}
            maxWidth={capWidth(width)}
            onCancel={() => patchOverlayState({ modelPicker: false })}
            onSelect={onModelSelect}
            sessionId={sid}
            t={theme}
          />
        </FloatBox>
      )
    })
  }

  if (overlay.skillsHub) {
    widgets.push({
      id: 'skills-hub',
      render: width => (
        <FloatBox color={theme.color.border}>
          <SkillsHub
            gw={gw}
            maxWidth={capWidth(width)}
            onClose={() => patchOverlayState({ skillsHub: false })}
            t={theme}
          />
        </FloatBox>
      )
    })
  }

  if (overlay.pager) {
    const pager = overlay.pager

    widgets.push({
      id: 'pager',
      render: width => (
        <FloatBox color={theme.color.border}>
          <Box flexDirection="column" paddingX={1} paddingY={1} width={capWidth(width)}>
            {pager.title && (
              <Box justifyContent="center" marginBottom={1}>
                <Text bold color={theme.color.primary}>
                  {pager.title}
                </Text>
              </Box>
            )}

            {pager.lines.slice(pager.offset, pager.offset + pagerPageSize).map((line, i) => (
              <Text key={i}>{line}</Text>
            ))}

            <Box marginTop={1}>
              <OverlayHint t={theme}>
                {pager.offset + pagerPageSize < pager.lines.length
                  ? `↑↓/jk line · Enter/Space/PgDn page · b/PgUp back · g/G top/bottom · Esc/q close (${Math.min(pager.offset + pagerPageSize, pager.lines.length)}/${pager.lines.length})`
                  : `end · ↑↓/jk · b/PgUp back · g top · Esc/q close (${pager.lines.length} lines)`}
              </OverlayHint>
            </Box>
          </Box>
        </FloatBox>
      ),
      span: fullSpan
    })
  }

  if (completions.length) {
    widgets.push({
      id: 'completions',
      render: width => (
        <FloatBox color={theme.color.primary}>
          <Box flexDirection="column" width={capWidth(width)}>
            {completions.slice(start, start + viewportSize).map((item, i) => {
              const active = start + i === compIdx

              return (
                <Box
                  backgroundColor={active ? theme.color.completionCurrentBg : undefined}
                  flexDirection="row"
                  key={`${start + i}:${item.text}:${item.display}:${item.meta ?? ''}`}
                  width="100%"
                >
                  <Text bold color={theme.color.label}>
                    {' '}
                    {item.display}
                  </Text>
                  {item.meta ? <Text color={theme.color.muted}> {item.meta}</Text> : null}
                </Box>
              )
            })}
          </Box>
        </FloatBox>
      ),
      span: fullSpan
    })
  }

  return (
    <Box alignItems="flex-start" bottom="100%" flexDirection="column" left={0} position="absolute" right={0}>
      <WidgetGrid cols={gridCols} maxColumns={gridMaxColumns} minColumnWidth={46} rowGap={0} widgets={widgets} />
    </Box>
  )
}
