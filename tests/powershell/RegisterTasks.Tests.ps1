#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# v2.3 step 10: the user-run task registration scripts check their arguments at registration time and, with
# -WhatIf, register nothing. Run: Invoke-Pester -Path tests/powershell -Output Detailed

BeforeAll {
    Set-StrictMode -Version Latest
    $script:Tools = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'tools'

    function Invoke-RegisterScript {
        param(
            [Parameter(Mandatory)][string]$Name,
            [string[]]$Arguments = @()
        )
        $path = Join-Path $script:Tools "$Name.ps1"
        $out = & powershell -NoProfile -NonInteractive -File $path @Arguments -WhatIf 2>&1 | Out-String
        [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $out }
    }
}

Describe 'register_campaign_task.ps1' {
    It 'runs campaign c2 with the v3 spaces on the pre-registered grid by default' {
        $r = Invoke-RegisterScript -Name 'register_campaign_task'
        $r.ExitCode | Should -Be 0
        $r.Output | Should -Match '--run-id c2 --grid preregistered --space-version 3'
        $r.Output | Should -Match 'Register per-user scheduled task'
    }

    It 'can still address the finished campaign c1 under its own space version' {
        $r = Invoke-RegisterScript -Name 'register_campaign_task' -Arguments @('-RunId', 'c1', '-SpaceVersion', '2')
        $r.ExitCode | Should -Be 0
        $r.Output | Should -Match '--run-id c1 --grid preregistered --space-version 2'
    }

    It 'refuses a space version that does not exist' {
        $r = Invoke-RegisterScript -Name 'register_campaign_task' -Arguments @('-SpaceVersion', '4')
        $r.ExitCode | Should -Not -Be 0
        # the console wraps the parameter name; the set itself stays on one line
        $r.Output | Should -Match 'does not belong to the set "2,3"'
    }

    It 'writes its report outside the repository (a tick must not rewrite a committed file)' {
        $r = Invoke-RegisterScript -Name 'register_campaign_task'
        $r.Output | Should -Match '--report "C:\\dev\\qr-data\\run\\campaign-c2\.md"'
    }

    It 'refuses an unknown grid mode before registering anything' {
        $r = Invoke-RegisterScript -Name 'register_campaign_task' -Arguments @('-Grid', 'everything')
        $r.ExitCode | Should -Not -Be 0
        $r.Output | Should -Match 'Grid'
        $r.Output | Should -Not -Match 'Register per-user scheduled task'
    }

    It 'refuses a timeframe outside 1h, 4h, 1d' {
        $r = Invoke-RegisterScript -Name 'register_campaign_task' -Arguments @('-Grid', 'cli', '-Timeframes', '1h,5m')
        $r.ExitCode | Should -Not -Be 0
        $r.Output | Should -Match "invalid value '5m'"
    }
}

Describe 'register_data_queue_task.ps1' {
    It 'runs one queue step per tick with a time budget that ends before the next tick' {
        $r = Invoke-RegisterScript -Name 'register_data_queue_task'
        $r.ExitCode | Should -Be 0
        $r.Output | Should -Match 'run_data_queue.py --max-hours 1.75'
    }

    It 'refuses a time budget that would overlap the next tick, the boundary included' {
        foreach ($case in @(@('60', '2'), @('120', '2'))) {
            $r = Invoke-RegisterScript -Name 'register_data_queue_task' -Arguments @('-RepeatMinutes', $case[0], '-MaxHours', $case[1])
            $r.ExitCode | Should -Not -Be 0
            $r.Output | Should -Match 'must end before the next tick'
        }
    }
}

Describe 'register_validator_task.ps1' {
    It 'runs the validator with a log file' {
        $r = Invoke-RegisterScript -Name 'register_validator_task'
        $r.ExitCode | Should -Be 0
        $r.Output | Should -Match 'run_validator.py --log-file'
    }

    It 'refuses a repeat interval under ten minutes' {
        $r = Invoke-RegisterScript -Name 'register_validator_task' -Arguments @('-RepeatMinutes', '5')
        $r.ExitCode | Should -Not -Be 0
        $r.Output | Should -Match 'ParameterArgumentValidationError'  # the parameter name may be wrapped in the output
    }
}
