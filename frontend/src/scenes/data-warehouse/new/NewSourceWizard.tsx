import { IconCheck } from '@posthog/icons'
import { LemonButton, LemonTable, LemonTag, Link } from '@posthog/lemon-ui'
import { BindLogic, useActions, useValues } from 'kea'
import { PageHeader } from 'lib/components/PageHeader'
import { FEATURE_FLAGS } from 'lib/constants'
import { featureFlagLogic } from 'lib/logic/featureFlagLogic'
import { useCallback, useEffect } from 'react'
import { DataWarehouseSourceIcon } from 'scenes/data-warehouse/settings/DataWarehouseSourceIcon'
import { SceneExport } from 'scenes/sceneTypes'

import { ManualLinkSourceType, SourceConfig } from '~/types'

import { DataWarehouseInitialBillingLimitNotice } from '../DataWarehouseInitialBillingLimitNotice'
import SchemaForm from '../external/forms/SchemaForm'
import SourceForm from '../external/forms/SourceForm'
import { SyncProgressStep } from '../external/forms/SyncProgressStep'
import { DatawarehouseTableForm } from '../new/DataWarehouseTableForm'
import { dataWarehouseTableLogic } from './dataWarehouseTableLogic'
import { sourceWizardLogic } from './sourceWizardLogic'

export const scene: SceneExport = {
    component: NewSourceWizardScene,
    logic: sourceWizardLogic,
}
export function NewSourceWizardScene(): JSX.Element {
    const { closeWizard } = useActions(sourceWizardLogic)

    return (
        <>
            <PageHeader
                buttons={
                    <>
                        <LemonButton
                            type="secondary"
                            center
                            data-attr="source-form-cancel-button"
                            onClick={closeWizard}
                        >
                            Cancel
                        </LemonButton>
                    </>
                }
            />
            <NewSourcesWizard />
        </>
    )
}

interface NewSourcesWizardProps {
    disableConnectedSources?: boolean
    onComplete?: () => void
}

export function NewSourcesWizard(props: NewSourcesWizardProps): JSX.Element {
    const { onComplete } = props
    const wizardLogic = sourceWizardLogic({ onComplete })

    const { modalTitle, modalCaption, isWrapped, currentStep, isLoading, canGoBack, canGoNext, nextButtonText } =
        useValues(wizardLogic)
    const { onBack, onSubmit, onClear } = useActions(wizardLogic)
    const { tableLoading: manualLinkIsLoading } = useValues(dataWarehouseTableLogic)

    useEffect(() => {
        return () => {
            onClear()
        }
    }, [onClear])

    const footer = useCallback(() => {
        if (currentStep === 1) {
            return null
        }

        return (
            <div className="mt-4 flex flex-row justify-end gap-2">
                {canGoBack && (
                    <LemonButton
                        type="secondary"
                        center
                        data-attr="source-modal-back-button"
                        onClick={onBack}
                        disabledReason={!canGoBack && 'You cant go back from here'}
                    >
                        Back
                    </LemonButton>
                )}
                <LemonButton
                    loading={isLoading || manualLinkIsLoading}
                    disabledReason={!canGoNext && 'You cant click next yet'}
                    type="primary"
                    center
                    onClick={() => onSubmit()}
                    data-attr="source-link"
                >
                    {nextButtonText}
                </LemonButton>
            </div>
        )
    }, [currentStep, canGoBack, onBack, isLoading, manualLinkIsLoading, canGoNext, nextButtonText, onSubmit])

    return (
        <>
            {!isWrapped && <DataWarehouseInitialBillingLimitNotice />}
            <>
                <h3>{modalTitle}</h3>
                <p>{modalCaption}</p>

                {currentStep === 1 ? (
                    <FirstStep {...props} />
                ) : currentStep === 2 ? (
                    <SecondStep />
                ) : currentStep === 3 ? (
                    <ThirdStep />
                ) : currentStep === 4 ? (
                    <FourthStep />
                ) : (
                    <div>Something went wrong...</div>
                )}

                {footer()}
            </>
        </>
    )
}

function FirstStep({ disableConnectedSources }: Pick<NewSourcesWizardProps, 'disableConnectedSources'>): JSX.Element {
    const { connectors, manualConnectors, addToHubspotButtonUrl } = useValues(sourceWizardLogic)
    const { selectConnector, toggleManualLinkFormVisible, onNext, setManualLinkingProvider } =
        useActions(sourceWizardLogic)
    const { featureFlags } = useValues(featureFlagLogic)

    const onClick = (sourceConfig: SourceConfig): void => {
        if (sourceConfig.name == 'Hubspot') {
            window.open(addToHubspotButtonUrl() as string)
        } else {
            selectConnector(sourceConfig)
        }
        onNext()
    }

    const onManualLinkClick = (manualLinkSource: ManualLinkSourceType): void => {
        toggleManualLinkFormVisible(true)
        setManualLinkingProvider(manualLinkSource)
    }

    const filteredConnectors = connectors.filter((n) => {
        return !(n.name === 'BigQuery' && !featureFlags[FEATURE_FLAGS.BIGQUERY_DWH])
    })

    return (
        <>
            <h2 className="mt-4">Managed by PostHog</h2>

            <p>
                Data will be synced to PostHog and regularly refreshed.{' '}
                <Link to="https://posthog.com/docs/data-warehouse/setup#stripe">Learn more</Link>
            </p>
            <LemonTable
                dataSource={filteredConnectors}
                loading={false}
                disableTableWhileLoading={false}
                columns={[
                    {
                        title: 'Source',
                        width: 0,
                        render: function (_, sourceConfig) {
                            return <DataWarehouseSourceIcon type={sourceConfig.name} />
                        },
                    },
                    {
                        title: 'Name',
                        key: 'name',
                        render: (_, sourceConfig) => (
                            <span className="font-semibold text-sm gap-1">
                                {sourceConfig.label ?? sourceConfig.name}
                            </span>
                        ),
                    },
                    {
                        key: 'actions',
                        width: 0,
                        render: (_, sourceConfig) => {
                            const isConnected = disableConnectedSources && sourceConfig.existingSource

                            return (
                                <div className="flex flex-row justify-end">
                                    {isConnected && (
                                        <LemonTag type="success" className="my-4" size="medium">
                                            <IconCheck />
                                            Connected
                                        </LemonTag>
                                    )}
                                    {!isConnected && (
                                        <LemonButton
                                            onClick={() => onClick(sourceConfig)}
                                            className="my-2"
                                            type="primary"
                                            disabledReason={
                                                disableConnectedSources && sourceConfig.existingSource
                                                    ? 'You have already connected this source'
                                                    : undefined
                                            }
                                        >
                                            Link
                                        </LemonButton>
                                    )}
                                </div>
                            )
                        },
                    },
                ]}
            />

            <h2 className="mt-4">Self-managed</h2>

            <p>
                Data will be queried directly from your data source that you manage.{' '}
                <Link to="https://posthog.com/docs/data-warehouse/setup#linking-a-custom-source">Learn more</Link>
            </p>
            <LemonTable
                dataSource={manualConnectors}
                loading={false}
                disableTableWhileLoading={false}
                columns={[
                    {
                        title: 'Source',
                        width: 0,
                        render: (_, sourceConfig) => <DataWarehouseSourceIcon type={sourceConfig.type} />,
                    },
                    {
                        title: 'Name',
                        key: 'name',
                        render: (_, sourceConfig) => (
                            <span className="font-semibold text-sm gap-1">{sourceConfig.name}</span>
                        ),
                    },
                    {
                        key: 'actions',
                        width: 0,
                        render: (_, sourceConfig) => (
                            <div className="flex flex-row justify-end">
                                <LemonButton
                                    onClick={() => onManualLinkClick(sourceConfig.type)}
                                    className="my-2"
                                    type="primary"
                                >
                                    Link
                                </LemonButton>
                            </div>
                        ),
                    },
                ]}
            />
        </>
    )
}

function SecondStep(): JSX.Element {
    const { selectedConnector } = useValues(sourceWizardLogic)

    return selectedConnector ? (
        <SourceForm sourceConfig={selectedConnector} />
    ) : (
        <BindLogic logic={dataWarehouseTableLogic} props={{ id: 'new' }}>
            <DatawarehouseTableForm />
        </BindLogic>
    )
}

function ThirdStep(): JSX.Element {
    return <SchemaForm />
}

function FourthStep(): JSX.Element {
    return <SyncProgressStep />
}
