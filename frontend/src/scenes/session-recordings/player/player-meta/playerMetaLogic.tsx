import { IconCursorClick, IconKeyboard, IconWarning } from '@posthog/icons'
import { eventWithTime } from '@posthog/rrweb-types'
import { actions, connect, kea, key, listeners, path, props, reducers, selectors } from 'kea'
import { loaders } from 'kea-loaders'
import api from 'lib/api'
import { PropertyFilterIcon } from 'lib/components/PropertyFilters/components/PropertyFilterIcon'
import { TaxonomicFilterGroupType } from 'lib/components/TaxonomicFilter/types'
import { lemonToast } from 'lib/lemon-ui/LemonToast'
import { getCoreFilterDefinition, getFirstFilterTypeFor } from 'lib/taxonomy'
import { ceilMsToClosestSecond, findLastIndex, humanFriendlyDuration, objectsEqual, percentage } from 'lib/utils'
import { COUNTRY_CODE_TO_LONG_NAME } from 'lib/utils/geography/country'
import posthog from 'posthog-js'
import { OverviewItem } from 'scenes/session-recordings/components/OverviewGrid'
import { TimestampFormat } from 'scenes/session-recordings/player/playerSettingsLogic'
import { sessionRecordingDataLogic } from 'scenes/session-recordings/player/sessionRecordingDataLogic'
import {
    sessionRecordingPlayerLogic,
    SessionRecordingPlayerLogicProps,
} from 'scenes/session-recordings/player/sessionRecordingPlayerLogic'

import { PersonType, PropertyFilterType } from '~/types'

import { SimpleTimeLabel } from '../../components/SimpleTimeLabel'
import { sessionRecordingsListPropertiesLogic } from '../../playlist/sessionRecordingsListPropertiesLogic'
import type { playerMetaLogicType } from './playerMetaLogicType'

const recordingPropertyKeys = ['click_count', 'keypress_count', 'console_error_count'] as const

export interface SessionSummaryResponse {
    content: string
}

export function countryTitleFrom(
    recordingProperties: Record<string, any> | undefined,
    personProperties?: Record<string, any> | undefined
): string {
    const props = recordingProperties || personProperties
    if (!props) {
        return ''
    }

    // these prop names are safe between recording and person properties
    // the "initial" person properties share the same name as the event properties
    const country = COUNTRY_CODE_TO_LONG_NAME[props['$geoip_country_code'] as keyof typeof COUNTRY_CODE_TO_LONG_NAME]
    const subdivision = props['$geoip_subdivision_1_name']
    const city = props['$geoip_city_name']

    return [city, subdivision, country].filter(Boolean).join(', ')
}

export const playerMetaLogic = kea<playerMetaLogicType>([
    path((key) => ['scenes', 'session-recordings', 'player', 'playerMetaLogic', key]),
    props({} as SessionRecordingPlayerLogicProps),
    key((props: SessionRecordingPlayerLogicProps) => `${props.playerKey}-${props.sessionRecordingId}`),
    connect((props: SessionRecordingPlayerLogicProps) => ({
        values: [
            sessionRecordingDataLogic(props),
            [
                'urls',
                'sessionPlayerData',
                'sessionEventsData',
                'sessionPlayerMetaData',
                'sessionPlayerMetaDataLoading',
                'snapshotsLoading',
                'windowIds',
                'trackedWindow',
            ],
            sessionRecordingPlayerLogic(props),
            ['scale', 'currentTimestamp', 'currentPlayerTime', 'currentSegment'],
            sessionRecordingsListPropertiesLogic,
            ['recordingPropertiesById', 'recordingPropertiesLoading'],
        ],
        actions: [
            sessionRecordingDataLogic(props),
            ['loadRecordingMetaSuccess', 'setTrackedWindow'],
            sessionRecordingsListPropertiesLogic,
            ['maybeLoadPropertiesForSessions'],
        ],
    })),
    actions({
        sessionSummaryFeedback: (feedback: 'good' | 'bad') => ({ feedback }),
    }),
    reducers(() => ({
        summaryHasHadFeedback: [
            false,
            {
                sessionSummaryFeedback: () => true,
            },
        ],
    })),
    loaders(({ props }) => ({
        sessionSummary: {
            summarizeSession: async (): Promise<SessionSummaryResponse | null> => {
                const id = props.sessionRecordingId || props.sessionRecordingData?.sessionRecordingId
                if (!id) {
                    return null
                }
                const response = await api.recordings.summarize(id)
                if (!response.content) {
                    lemonToast.warning('Unable to load session summary')
                }
                return { content: response.content }
            },
        },
    })),
    selectors(() => ({
        loading: [
            (s) => [s.sessionPlayerMetaDataLoading, s.snapshotsLoading, s.recordingPropertiesLoading],
            (sessionPlayerMetaDataLoading, snapshotsLoading, recordingPropertiesLoading) =>
                sessionPlayerMetaDataLoading || snapshotsLoading || recordingPropertiesLoading,
        ],
        sessionPerson: [
            (s) => [s.sessionPlayerData],
            (playerData): PersonType | null => {
                return playerData?.person ?? null
            },
        ],
        resolution: [
            (s) => [s.sessionPlayerData, s.currentTimestamp, s.currentSegment],
            (sessionPlayerData, currentTimestamp, currentSegment): { width: number; height: number } | null => {
                // Find snapshot to pull resolution from
                if (!currentTimestamp) {
                    return null
                }
                const snapshots = sessionPlayerData.snapshotsByWindowId[currentSegment?.windowId ?? ''] ?? []

                const currIndex = findLastIndex(
                    snapshots,
                    (s: eventWithTime) => s.timestamp < currentTimestamp && (s.data as any).width
                )

                if (currIndex === -1) {
                    return null
                }
                const snapshot = snapshots[currIndex]
                return {
                    width: snapshot.data?.['width'],
                    height: snapshot.data?.['height'],
                }
            },
            {
                resultEqualityCheck: (prev, next) => {
                    // Only update if the resolution values have changed (not the object reference)
                    // stops PlayerMeta from re-rendering on every player position
                    return objectsEqual(prev, next)
                },
            },
        ],
        resolutionDisplay: [
            (s) => [s.resolution],
            (resolution) => {
                return `${resolution?.width || '--'} x ${resolution?.height || '--'}`
            },
        ],
        scaleDisplay: [
            (s) => [s.scale],
            (scale) => {
                return `${percentage(scale, 1, true)}`
            },
        ],
        startTime: [
            (s) => [s.sessionPlayerData],
            (sessionPlayerData) => {
                return sessionPlayerData.start ?? null
            },
        ],

        endTime: [
            (s) => [s.sessionPlayerData],
            (sessionPlayerData) => {
                return sessionPlayerData.end ?? null
            },
        ],

        currentWindowIndex: [
            (s) => [s.windowIds, s.currentSegment],
            (windowIds, currentSegment) => {
                const index = windowIds.findIndex((windowId) =>
                    currentSegment?.windowId ? windowId === currentSegment?.windowId : -1
                )
                return index === -1 ? 1 : index + 1
            },
        ],
        lastUrl: [
            (s) => [s.urls, s.sessionPlayerMetaData, s.currentTimestamp],
            (urls, sessionPlayerMetaData, currentTimestamp): string | undefined => {
                if (!urls.length || !currentTimestamp) {
                    return sessionPlayerMetaData?.start_url ?? undefined
                }

                // Go through the events in reverse to find the latest pageview
                for (let i = urls.length - 1; i >= 0; i--) {
                    const urlTimestamp = urls[i]
                    if (i === 0 || urlTimestamp.timestamp < currentTimestamp) {
                        return urlTimestamp.url
                    }
                }
            },
        ],
        lastPageviewEvent: [
            (s) => [s.sessionEventsData, s.currentPlayerTime],
            (sessionEventsData, currentPlayerTime) => {
                const playerTimeClosestSecond = ceilMsToClosestSecond(currentPlayerTime ?? 0)

                if (!sessionEventsData?.length) {
                    return null
                }

                // Go through the events in reverse to find the latest pageview
                for (let i = sessionEventsData.length - 1; i >= 0; i--) {
                    const event = sessionEventsData[i]
                    if (
                        (event.event === '$screen' || event.event === '$pageview') &&
                        (event.playerTime ?? 0) < playerTimeClosestSecond
                    ) {
                        return event
                    }
                }
            },
        ],
        overviewItems: [
            (s) => [s.sessionPlayerMetaData, s.startTime, s.recordingPropertiesById],
            (sessionPlayerMetaData, startTime, recordingPropertiesById) => {
                const items: OverviewItem[] = []
                if (startTime) {
                    items.push({
                        label: 'Start',
                        value: (
                            <SimpleTimeLabel
                                muted={false}
                                size="small"
                                timestampFormat={TimestampFormat.UTC}
                                startTime={startTime}
                            />
                        ),
                        type: 'text',
                    })
                }
                if (sessionPlayerMetaData?.recording_duration) {
                    items.push({
                        label: 'Duration',
                        value: humanFriendlyDuration(sessionPlayerMetaData.recording_duration),
                        type: 'text',
                    })
                }

                recordingPropertyKeys.forEach((property) => {
                    if (sessionPlayerMetaData?.[property]) {
                        items.push({
                            icon:
                                property === 'click_count' ? (
                                    <IconCursorClick />
                                ) : property === 'keypress_count' ? (
                                    <IconKeyboard />
                                ) : property === 'console_error_count' ? (
                                    <IconWarning />
                                ) : undefined,
                            label:
                                getCoreFilterDefinition(property, TaxonomicFilterGroupType.Replay)?.label ?? property,
                            value: `${sessionPlayerMetaData[property]}`,
                            type: 'text',
                        })
                    }
                })

                const recordingProperties = sessionPlayerMetaData?.id
                    ? recordingPropertiesById[sessionPlayerMetaData?.id] || {}
                    : {}
                const personProperties = sessionPlayerMetaData?.person?.properties ?? {}

                const propertiesToUse = Object.keys(recordingProperties).length ? recordingProperties : personProperties
                if (propertiesToUse['$os_name'] && propertiesToUse['$os']) {
                    // we don't need both, prefer $os_name in case mobile sends better value in that field
                    delete propertiesToUse['$os']
                }
                Object.entries(propertiesToUse).forEach(([property, value]) => {
                    if (value == null) {
                        return
                    }
                    if (property === '$geoip_subdivision_1_name' || property === '$geoip_city_name') {
                        // they're just shown in the title for Country
                        return
                    }

                    const propertyType = recordingProperties[property]
                        ? // HogQL query can return multiple types, so we need to check
                          // but if it doesn't match a core definition it must be an event property
                          getFirstFilterTypeFor(property) || TaxonomicFilterGroupType.EventProperties
                        : TaxonomicFilterGroupType.PersonProperties

                    items.push({
                        icon: (
                            <PropertyFilterIcon
                                type={
                                    propertyType === TaxonomicFilterGroupType.EventProperties
                                        ? PropertyFilterType.Event
                                        : TaxonomicFilterGroupType.SessionProperties
                                        ? PropertyFilterType.Session
                                        : PropertyFilterType.Person
                                }
                            />
                        ),
                        label: getCoreFilterDefinition(property, propertyType)?.label ?? property,
                        value,
                        keyTooltip:
                            propertyType === TaxonomicFilterGroupType.EventProperties
                                ? 'Event property'
                                : TaxonomicFilterGroupType.SessionProperties
                                ? 'Session property'
                                : 'Person property',
                        valueTooltip:
                            property === '$geoip_country_code' && value in COUNTRY_CODE_TO_LONG_NAME
                                ? countryTitleFrom(recordingProperties, personProperties)
                                : value,
                        type: 'property',
                        property,
                    })
                })

                return items
            },
        ],
    })),
    listeners(({ actions, values, props }) => ({
        loadRecordingMetaSuccess: () => {
            if (values.sessionPlayerMetaData) {
                actions.maybeLoadPropertiesForSessions([values.sessionPlayerMetaData])
            }
        },
        sessionSummaryFeedback: ({ feedback }) => {
            posthog.capture('session summary feedback', {
                feedback,
                session_summary: values.sessionSummary,
                summarized_session_id: props.sessionRecordingId,
            })
        },
    })),
])
