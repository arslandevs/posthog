import { Hub } from '~/src/types'

import { runInstrumentedFunction } from '../../main/utils'
import { LegacyPluginExecutorService } from '../services/legacy-plugin-executor.service'
import { HogFunctionInvocation, HogFunctionInvocationResult, HogFunctionTypeType } from '../types'
import { CdpCyclotronWorker } from './cdp-cyclotron-worker.consumer'

/**
 * NOTE: This is a consumer to take care of legacy plugins.
 */
export class CdpCyclotronWorkerPlugins extends CdpCyclotronWorker {
    protected name = 'CdpCyclotronWorkerPlugins'
    protected queue = 'plugin' as const
    protected hogTypes: HogFunctionTypeType[] = ['destination']
    private pluginExecutor: LegacyPluginExecutorService

    constructor(hub: Hub) {
        super(hub)
        this.pluginExecutor = new LegacyPluginExecutorService(hub)
    }

    public async processInvocations(invocations: HogFunctionInvocation[]): Promise<HogFunctionInvocationResult[]> {
        // Plugins fire fetch requests and so need to be run in true parallel
        return await Promise.all(
            invocations.map((item) =>
                runInstrumentedFunction({
                    statsKey: `cdpConsumer.handleEachBatch.executePluginInvocation`,
                    func: async () => await this.pluginExecutor.execute(item),
                })
            )
        )
    }
}
