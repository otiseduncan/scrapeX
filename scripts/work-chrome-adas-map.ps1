param(
    [ValidateSet("status", "open", "inspect", "lookup", "read-current", "details", "close-details", "open-report", "download-report", "open-oe-link", "read-window", "capture-oe-si", "return-to-adas")]
    [string]$Action = "status",

    [string]$RoNumber = "",

    [string]$SavePath = "",

    [string]$InspectionId = "",

    [string]$HomeUrl = "",

    [string]$ChromeProfile = "",

    [int]$ExpectedYear = 0,

    [string]$ExpectedMake = "",

    [string]$ExpectedModel = "",

    [string]$RequirementLabel = "",

    [string]$WindowTitleContains = ""
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class NativeWindow {
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("kernel32.dll")]
    public static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    [DllImport("user32.dll")]
    public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int X, int Y);

    [DllImport("user32.dll")]
    public static extern bool SetProcessDpiAwarenessContext(IntPtr dpiContext);

    [DllImport("user32.dll")]
    public static extern bool SetProcessDPIAware();

    [DllImport("user32.dll")]
    public static extern void mouse_event(
        uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo
    );
}
"@

# UI Automation bounding rectangles are device-pixel coordinates. PowerShell is
# otherwise DPI-virtualized on scaled Windows desktops, which can make a valid
# UIA rectangle miss the visible web control when passed to SetCursorPos.
$DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = [IntPtr](-4)
try {
    [NativeWindow]::SetProcessDpiAwarenessContext(
        $DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    ) | Out-Null
}
catch {
    try { [NativeWindow]::SetProcessDPIAware() | Out-Null } catch {}
}

$SW_RESTORE = 9
$MOUSEEVENTF_LEFTDOWN = 0x0002
$MOUSEEVENTF_LEFTUP   = 0x0004

function Element-Data {
    param([System.Windows.Automation.AutomationElement]$Element)

    if ($null -eq $Element) { return $null }

    try {
        $rect = $Element.Current.BoundingRectangle
        $patterns = @()
        try {
            $patterns = @(
                $Element.GetSupportedPatterns() |
                ForEach-Object { [string]$_.ProgrammaticName }
            )
        }
        catch {}

        $legacyAction = $null
        $legacyValue = $null
        try {
            $legacy = $Element.GetCurrentPattern(
                [System.Windows.Automation.LegacyIAccessiblePattern]::Pattern
            )
            if ($null -ne $legacy) {
                $legacyAction = [string]$legacy.Current.DefaultAction
                $legacyValue = [string]$legacy.Current.Value
            }
        }
        catch {}

        $runtimeId = $null
        try { $runtimeId = ($Element.GetRuntimeId() -join ".") } catch {}

        return [pscustomobject]@{
            name          = [string]$Element.Current.Name
            automation_id = [string]$Element.Current.AutomationId
            class_name    = [string]$Element.Current.ClassName
            control_type  = [string]$Element.Current.ControlType.ProgrammaticName
            enabled       = [bool]$Element.Current.IsEnabled
            offscreen     = [bool]$Element.Current.IsOffscreen
            process_id    = [int]$Element.Current.ProcessId
            runtime_id    = $runtimeId
            patterns      = $patterns
            default_action = $legacyAction
            value         = $legacyValue
            rect          = @{
                left   = [double]$rect.Left
                top    = [double]$rect.Top
                width  = [double]$rect.Width
                height = [double]$rect.Height
            }
        }
    }
    catch {
        return $null
    }
}

function Get-Chrome-Windows {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $children = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Children,
        [System.Windows.Automation.Condition]::TrueCondition
    )

    $result = @()

    foreach ($element in $children) {
        try {
            if ($element.Current.ClassName -ne "Chrome_WidgetWin_1") {
                continue
            }

            $processId = [int]$element.Current.ProcessId
            $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($null -eq $process -or $process.ProcessName -ne "chrome") {
                continue
            }

            $result += [pscustomobject]@{
                element    = $element
                title      = [string]$element.Current.Name
                process_id = $processId
                handle     = [int64]$element.Current.NativeWindowHandle
            }
        }
        catch {}
    }

    return @($result)
}

function Resolve-Chrome-Executable {
    # The sign-in handoff must open real Google Chrome (the managed work
    # browser), never whatever the default http handler happens to be.
    $candidates = @()
    foreach ($registryPath in @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    )) {
        try {
            $item = Get-ItemProperty -Path $registryPath -ErrorAction Stop
            $value = [string]$item.'(default)'
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                $candidates += $value.Trim('"')
            }
        }
        catch {}
    }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if (-not [string]::IsNullOrWhiteSpace($base)) {
            $candidates += (Join-Path $base "Google\Chrome\Application\chrome.exe")
        }
    }
    foreach ($candidate in $candidates) {
        try {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                return $candidate
            }
        }
        catch {}
    }
    return $null
}

function Get-All-Top-Windows {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $children = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Children,
        [System.Windows.Automation.Condition]::TrueCondition
    )

    $result = @()
    foreach ($element in $children) {
        try {
            $result += [pscustomobject]@{
                element     = $element
                title       = [string]$element.Current.Name
                class_name  = [string]$element.Current.ClassName
                process_id  = [int]$element.Current.ProcessId
                handle      = [int64]$element.Current.NativeWindowHandle
            }
        }
        catch {}
    }

    return @($result)
}

function Find-Save-Dialog {
    param(
        [System.Windows.Automation.AutomationElement]$ChromeRoot,
        [int]$ChromeProcessId
    )

    # Prefer a genuine separate top-level common dialog (native "#32770" or a
    # window titled "Save As"), which is how classic OS Save dialogs appear.
    $windows = @(Get-All-Top-Windows)
    $topLevel = @(
        $windows |
        Where-Object {
            $_.process_id -eq $ChromeProcessId -and
            (
                $_.class_name -eq "#32770" -or
                $_.title -match "(?i)^Save\s*$|^Save\b.*\bAs\s*$"
            )
        }
    )
    if ($topLevel.Count -gt 0) {
        return [pscustomobject]@{ element = $topLevel[0].element; title = $topLevel[0].title; nested = $false }
    }

    # Chrome's own Views-toolkit Save dialog is instead nested INSIDE the
    # browser window's own UI Automation tree as a ControlType.Window, not a
    # sibling window under the desktop root -- so it never shows up above.
    if ($null -eq $ChromeRoot) { return $null }

    $desc = Descendants -Root $ChromeRoot
    foreach ($element in $desc) {
        try {
            if ($element.Current.ControlType -ne [System.Windows.Automation.ControlType]::Window) {
                continue
            }
            if ([string]$element.Current.Name -match "(?i)^Save\s*$|^Save\b.*\bAs\s*$") {
                return [pscustomobject]@{ element = $element; title = [string]$element.Current.Name; nested = $true }
            }
        }
        catch {}
    }

    return $null
}

function Select-AdasMap-Window {
    param($Windows)

    $preferred = @(
        $Windows |
        Where-Object {
            $_.title -match "(?i)\bADAS\b|ADAS\s*Map|gerber\.adasmap|opus\.adasmap"
        }
    )

    if ($preferred.Count -gt 0) {
        # Prefer the file-grid window over a login page if both are open.
        $grid = @(
            $preferred |
            Where-Object {
                $_.title -notmatch "(?i)login"
            }
        )
        if ($grid.Count -gt 0) {
            return $grid[0]
        }
        return $preferred[0]
    }

    return $null
}

function Close-Alldata-Tab-And-Return-To-Adas {
    # Otis caught this live: capture-oe-si was leaving every ALLDATA tab it
    # opened sitting there indefinitely (Return-To-Adas-Tab only switches
    # which tab is active, it never closes anything) -- tabs from a whole
    # batch run all accumulate in the same window, and that pile-up is what
    # was destabilizing later lookups/business-switches, not just clutter.
    # Close the tab this specific capture opened (only if it's genuinely an
    # ALLDATA tab, so this never risks closing the wrong thing when
    # navigation was never actually confirmed) before switching back.
    param([Parameter(Mandatory)]$Handle)

    try {
        $windowsNow = @(Get-Chrome-Windows)
        $current = $windowsNow | Where-Object { $_.handle -eq $Handle } | Select-Object -First 1
        if ($null -ne $current -and [string]$current.title -match '(?i)alldata') {
            Bring-To-Front -Target $current | Out-Null
            Start-Sleep -Milliseconds 300
            [System.Windows.Forms.SendKeys]::SendWait("^w")
            Start-Sleep -Milliseconds 500
        }
    }
    catch {}

    return Return-To-Adas-Tab
}

function Return-To-Adas-Tab {
    # Switching an ALLDATA-focused window back to the ADAS Map tab it came
    # from. A click into a per-requirement OE link's own ALLDATA link opens
    # a genuine new tab and focuses it -- the original ADAS Map tab is not
    # closed, just left inactive, and Chrome can even memory-discard it
    # while waiting. Re-navigating by URL instead lands on a fresh login
    # page and risks the shared authenticated session -- confirmed the
    # wrong move directly. Chrome's own tab-search overlay lists every tab
    # regardless of discard state and switching via it preserves the
    # session, so that is the only recovery path used here.
    $windows = @(Get-Chrome-Windows)
    $adasWindow = Select-AdasMap-Window -Windows $windows
    if ($null -ne $adasWindow -and $adasWindow.title -notmatch '(?i)login') {
        # Already showing ADAS Map (not stuck on a login page) -- nothing to do.
        return $true
    }

    # More than one top-level Chrome window can be open at once (observed
    # directly: ALLDATA Repair, frontend, X Omni simultaneously). Only
    # search windows actually showing ALLDATA -- those are the ones that
    # could plausibly hold the ADAS Map tab this recovery is for. X Omni
    # and the CIQ frontend are this same operator's other own app windows
    # (also Chrome-hosted), not general browsing windows with unrelated
    # tabs to click through; clicking around in them here would be a real,
    # unrelated disruption, not a recovery.
    $searchWindows = @($windows | Where-Object { $_.title -match '(?i)alldata' })
    foreach ($candidateWindow in $searchWindows) {
        Bring-To-Front -Target $candidateWindow | Out-Null
        Start-Sleep -Milliseconds 300

        $desc = @(Descendants -Root $candidateWindow.element)
        $tabSearchBtn = $desc | Where-Object {
            -not $_.Current.IsOffscreen -and [string]$_.Current.Name -eq "Tab search"
        } | Select-Object -First 1
        if ($null -eq $tabSearchBtn) { continue }
        if (-not (Click-ElementCenter -Element $tabSearchBtn)) { continue }
        Start-Sleep -Milliseconds 500

        $desc2 = @(Descendants -Root $candidateWindow.element)
        $adasEntry = $desc2 | Where-Object {
            -not $_.Current.IsOffscreen -and [string]$_.Current.Name -match '(?i)^ADAS\b' -and
            [string]$_.Current.Name -notmatch '(?i)login'
        } | Select-Object -First 1
        if ($null -eq $adasEntry) {
            # Close whatever the tab-search overlay opened on this window
            # before moving on to the next candidate.
            try { [System.Windows.Forms.SendKeys]::SendWait("{ESC}") } catch {}
            continue
        }
        $clicked = Click-ElementCenter -Element $adasEntry
        Start-Sleep -Milliseconds 1200

        $windowsAfter = @(Get-Chrome-Windows)
        $adasAfter = Select-AdasMap-Window -Windows $windowsAfter
        if ($clicked -and $null -ne $adasAfter -and $adasAfter.title -notmatch '(?i)login') {
            return $true
        }
    }
    return $false
}

function Bring-To-Front {
    param($Target)

    try {
        $hWnd = [IntPtr]$Target.handle

        if ([NativeWindow]::IsIconic($hWnd)) {
            [NativeWindow]::ShowWindowAsync($hWnd, $SW_RESTORE) | Out-Null
            Start-Sleep -Milliseconds 200
        }

        # SetForegroundWindow's own return value being truthy does not mean
        # it worked -- confirmed live, this is very likely the actual root
        # cause of most navigation-confirmation failures downstream: Windows
        # silently refuses a foreground-focus steal from a background
        # process (each Action here is its own fresh powershell.exe
        # subprocess, never the currently-active app) unless the caller's
        # thread input is attached to the current foreground thread first --
        # a standard, well-documented workaround, not exotic. Attach, call,
        # detach, then verify by asking Windows what the foreground window
        # actually is now instead of trusting the call's own report.
        $currentFg = [NativeWindow]::GetForegroundWindow()
        if ($currentFg -ne $hWnd) {
            [uint32]$fgProcessId = 0
            [uint32]$targetProcessId = 0
            $fgThreadId = [NativeWindow]::GetWindowThreadProcessId($currentFg, [ref]$fgProcessId)
            $targetThreadId = [NativeWindow]::GetWindowThreadProcessId($hWnd, [ref]$targetProcessId)
            $curThreadId = [NativeWindow]::GetCurrentThreadId()
            $attached = $false
            if ($fgThreadId -ne 0 -and $fgThreadId -ne $curThreadId) {
                $attached = [NativeWindow]::AttachThreadInput($curThreadId, $fgThreadId, $true)
            }
            try {
                [NativeWindow]::SetForegroundWindow($hWnd) | Out-Null
            }
            finally {
                if ($attached) {
                    [NativeWindow]::AttachThreadInput($curThreadId, $fgThreadId, $false) | Out-Null
                }
            }
        }
        Start-Sleep -Milliseconds 250
        return ([NativeWindow]::GetForegroundWindow() -eq $hWnd)
    }
    catch {
        return $false
    }
}

function Descendants {
    param([System.Windows.Automation.AutomationElement]$Root)

    return $Root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
}

function Visible-Control-Data {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [int]$Limit = 350
    )

    $items = @()
    $desc = Descendants -Root $Root

    foreach ($element in $desc) {
        if ($items.Count -ge $Limit) { break }

        try {
            $name = [string]$element.Current.Name
            $automationId = [string]$element.Current.AutomationId

            if (
                [string]::IsNullOrWhiteSpace($name) -and
                [string]::IsNullOrWhiteSpace($automationId)
            ) {
                continue
            }

            if ($element.Current.IsOffscreen) {
                continue
            }

            $data = Element-Data -Element $element
            if ($null -ne $data) {
                $items += $data
            }
        }
        catch {}
    }

    return @($items)
}


function Find-Web-Document {
    param([System.Windows.Automation.AutomationElement]$ChromeRoot)

    $desc = Descendants -Root $ChromeRoot
    $docs = @()

    foreach ($element in $desc) {
        try {
            if ($element.Current.ControlType -ne [System.Windows.Automation.ControlType]::Document) {
                continue
            }
            if ($element.Current.IsOffscreen) { continue }

            $rect = $element.Current.BoundingRectangle
            if ($rect.Width -lt 300 -or $rect.Height -lt 200) { continue }

            $docs += [pscustomobject]@{
                element = $element
                area = [double]($rect.Width * $rect.Height)
            }
        }
        catch {}
    }

    if ($docs.Count -eq 0) { return $null }

    return (
        $docs |
        Sort-Object area -Descending |
        Select-Object -First 1
    ).element
}

function Document-Snapshot {
    param(
        [System.Windows.Automation.AutomationElement]$Document,
        [int]$Limit = 1500
    )

    $values = @()
    if ($null -eq $Document) {
        return @{ values = @(); count = 0 }
    }

    $desc = Descendants -Root $Document
    foreach ($element in $desc) {
        if ($values.Count -ge $Limit) { break }
        try {
            if ($element.Current.IsOffscreen) { continue }
            $name = [string]$element.Current.Name
            if (-not [string]::IsNullOrWhiteSpace($name)) {
                $values += $name
            }
        }
        catch {}
    }

    $unique = @($values | Select-Object -Unique)
    return @{ values = $unique; count = $unique.Count }
}

function Snapshot-Signature {
    param($Snapshot)

    $joined = ($Snapshot.values | Select-Object -First 180) -join "|"
    if ([string]::IsNullOrWhiteSpace($joined)) { return "" }

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($joined)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString($sha.ComputeHash($bytes)).Replace("-", "")
    }
    finally {
        $sha.Dispose()
    }
}

function Find-Search-Edit {
    param([System.Windows.Automation.AutomationElement]$Root)

    $desc = Descendants -Root $Root
    $edits = @()

    foreach ($element in $desc) {
        try {
            if (
                $element.Current.ControlType -ne
                [System.Windows.Automation.ControlType]::Edit
            ) {
                continue
            }

            if ($element.Current.IsOffscreen -or -not $element.Current.IsEnabled) {
                continue
            }

            $data = Element-Data -Element $element
            if ($null -eq $data) { continue }

            $edits += [pscustomobject]@{
                element = $element
                data    = $data
            }
        }
        catch {}
    }

    $named = @(
        $edits |
        Where-Object {
            $_.data.name -match "(?i)search" -or
            $_.data.automation_id -match "(?i)search"
        }
    )
    if ($named.Count -gt 0) {
        return $named[0]
    }

    $pageEdits = @(
        $edits |
        Where-Object {
            $_.data.rect.top -gt 90 -and
            $_.data.rect.width -gt 100
        } |
        Sort-Object { $_.data.rect.top }, { $_.data.rect.left }
    )
    if ($pageEdits.Count -gt 0) {
        return $pageEdits[0]
    }

    return $null
}

function Set-Element-Value {
    param(
        [System.Windows.Automation.AutomationElement]$Element,
        [string]$Value
    )

    try {
        $pattern = $Element.GetCurrentPattern(
            [System.Windows.Automation.ValuePattern]::Pattern
        )

        if ($null -ne $pattern -and -not $pattern.Current.IsReadOnly) {
            $pattern.SetValue($Value)
            return $true
        }
    }
    catch {}

    # Only focus the actual Edit control, never the Chrome root.
    try {
        $Element.SetFocus()
        Start-Sleep -Milliseconds 120
        [System.Windows.Forms.SendKeys]::SendWait("^a")
        [System.Windows.Forms.SendKeys]::SendWait($Value)
        return $true
    }
    catch {}

    # Final fallback: click center of the input by its accessible rectangle.
    try {
        $rect = $Element.Current.BoundingRectangle
        if ($rect.Width -gt 0 -and $rect.Height -gt 0) {
            $x = [int]($rect.Left + ($rect.Width / 2))
            $y = [int]($rect.Top + ($rect.Height / 2))

            [NativeWindow]::SetCursorPos($x, $y) | Out-Null
            [NativeWindow]::mouse_event(
                $MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero
            )
            [NativeWindow]::mouse_event(
                $MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero
            )

            Start-Sleep -Milliseconds 150
            [System.Windows.Forms.SendKeys]::SendWait("^a")
            [System.Windows.Forms.SendKeys]::SendWait($Value)
            return $true
        }
    }
    catch {}

    return $false
}



function Invoke-LegacyDefaultAction {
    param(
        [System.Windows.Automation.AutomationElement]$Element
    )

    if ($null -eq $Element) {
        return $false
    }

    try {
        $legacy = $Element.GetCurrentPattern(
            [System.Windows.Automation.LegacyIAccessiblePattern]::Pattern
        )

        if ($null -ne $legacy) {
            $legacy.DoDefaultAction()
            return $true
        }
    }
    catch {}

    return $false
}

function Find-View-Candidates {
    param(
        [System.Windows.Automation.AutomationElement]$Root
    )

    # Deliberately no bounding-rectangle filter here. Chrome exposes the
    # ADAS Map "view" control as a DataItem grid cell (custom-column-60)
    # rather than a normal hyperlink, and its accessibility rectangle can be
    # degenerate even though the control is visible and clickable. Rectangle
    # usability only matters later, for the physical-click activation
    # fallback -- it must not be used to decide whether the control exists.
    #
    # NOTE: the accumulator is deliberately NOT named $matches. PowerShell
    # variable names are case-insensitive, so $matches is the same variable
    # as the automatic $Matches populated by -match/-notmatch. Using it here
    # would let every regex test below silently overwrite the accumulator.
    $found = @()
    $desc = Descendants -Root $Root

    foreach ($element in $desc) {
        try {
            if ($element.Current.IsOffscreen) { continue }
            if ([string]$element.Current.Name -notmatch "(?i)^\s*view\s*$") { continue }

            $found += $element
        }
        catch {}
    }

    return @($found)
}

function Find-Best-View-Control {
    param(
        [System.Windows.Automation.AutomationElement]$Root
    )

    # NOTE: deliberately not named $matches -- see Find-View-Candidates for
    # why that collides with the automatic $Matches variable populated by
    # -match/-notmatch and silently corrupts the accumulator.
    $found = @()
    $desc = Descendants -Root $Root

    foreach ($element in $desc) {
        try {
            # Chrome often exposes web text cells with IsEnabled=false even
            # though the rendered text is clickable. For View, trust visibility
            # and a real screen rectangle instead of the Enabled bit.
            if ($element.Current.IsOffscreen) {
                continue
            }

            if ([string]$element.Current.Name -notmatch "(?i)^\s*view\s*$") {
                continue
            }

            $rect = $element.Current.BoundingRectangle
            if (
                $rect.Width -le 0 -or
                $rect.Height -le 0 -or
                [double]::IsInfinity($rect.Left) -or
                [double]::IsInfinity($rect.Top)
            ) {
                continue
            }

            $type = $element.Current.ControlType
            $score = 0

            if ($type -eq [System.Windows.Automation.ControlType]::Hyperlink) {
                $score += 100
            }
            elseif ($type -eq [System.Windows.Automation.ControlType]::Text) {
                $score += 80
            }
            elseif ($type -eq [System.Windows.Automation.ControlType]::Button) {
                $score += 70
            }
            elseif ($type -eq [System.Windows.Automation.ControlType]::DataItem) {
                $score += 30
            }

            # Prefer the smallest matching leaf. Chrome frequently exposes both
            # the clickable text and the larger grid cell with the same name.
            $area = [double]($rect.Width * $rect.Height)
            if ($area -gt 0) {
                $score += [Math]::Max(0, 50 - [Math]::Log10($area + 1) * 10)
            }

            $found += [pscustomobject]@{
                element = $element
                score = $score
                area = $area
                data = Element-Data -Element $element
            }
        }
        catch {}
    }

    if ($found.Count -eq 0) {
        # Last-resort fallback: walk the document again and accept any visible
        # element named View with a usable rectangle, regardless of control type.
        foreach ($element in $desc) {
            try {
                if ($element.Current.IsOffscreen) { continue }
                if ([string]$element.Current.Name -notmatch "(?i)^\s*view\s*$") { continue }

                $rect = $element.Current.BoundingRectangle
                if (
                    $rect.Width -gt 0 -and
                    $rect.Height -gt 0 -and
                    -not [double]::IsInfinity($rect.Left) -and
                    -not [double]::IsInfinity($rect.Top)
                ) {
                    return [pscustomobject]@{
                        element = $element
                        score = 1
                        area = [double]($rect.Width * $rect.Height)
                        data = Element-Data -Element $element
                    }
                }
            }
            catch {}
        }
        return $null
    }

    return (
        $found |
        Sort-Object -Property @(
            @{ Expression = "score"; Descending = $true },
            @{ Expression = "area"; Ascending = $true }
        ) |
        Select-Object -First 1
    )
}


function Element-Key {
    param([System.Windows.Automation.AutomationElement]$Element)

    if ($null -eq $Element) { return $null }
    try { return ($Element.GetRuntimeId() -join ".") } catch {}
    try {
        return (
            ([string]$Element.Current.ControlType.ProgrammaticName) + "|" +
            ([string]$Element.Current.AutomationId) + "|" +
            ([string]$Element.Current.Name) + "|" +
            ([string]$Element.Current.BoundingRectangle)
        )
    }
    catch { return $null }
}


function Ancestor-Chain {
    param(
        [System.Windows.Automation.AutomationElement]$Element,
        [int]$Limit = 18
    )

    $result = @()
    if ($null -eq $Element) { return $result }

    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $cursor = $Element
    for ($depth = 0; $depth -lt $Limit -and $null -ne $cursor; $depth++) {
        $result += [pscustomobject]@{
            element = $cursor
            key = Element-Key -Element $cursor
            depth = $depth
            data = Element-Data -Element $cursor
        }
        try { $cursor = $walker.GetParent($cursor) } catch { break }
    }
    return @($result)
}


function Deepest-Common-Ancestor {
    param(
        $Elements,
        [System.Windows.Automation.AutomationElement]$ExcludedRoot = $null,
        [int]$Limit = 30
    )

    $items = @($Elements | Where-Object { $null -ne $_ })
    if ($items.Count -lt 2) { return $null }

    $chains = @()
    foreach ($element in $items) {
        $chain = @(Ancestor-Chain -Element $element -Limit $Limit)
        if ($chain.Count -eq 0) { return $null }
        $lookup = @{}
        foreach ($node in $chain) {
            if ($node.key) { $lookup[$node.key] = $node }
        }
        $chains += ,@{ chain = $chain; lookup = $lookup }
    }

    $excludedKey = Element-Key -Element $ExcludedRoot
    foreach ($node in $chains[0].chain) {
        if (-not $node.key -or ($excludedKey -and $node.key -eq $excludedKey)) { continue }
        $shared = $true
        for ($i = 1; $i -lt $chains.Count; $i++) {
            if (-not $chains[$i].lookup.ContainsKey($node.key)) {
                $shared = $false
                break
            }
        }
        if ($shared) { return $node.element }
    }
    return $null
}


function Visible-Names-In-Scope {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [int]$Limit = 600
    )

    $values = @()
    if ($null -eq $Root) { return $values }
    try {
        $rootName = [string]$Root.Current.Name
        if (-not [string]::IsNullOrWhiteSpace($rootName)) { $values += $rootName }
    }
    catch {}
    foreach ($element in (Descendants -Root $Root)) {
        if ($values.Count -ge $Limit) { break }
        try {
            if ($element.Current.IsOffscreen) { continue }
            $name = [string]$element.Current.Name
            if (-not [string]::IsNullOrWhiteSpace($name)) { $values += $name }
        }
        catch {}
    }
    return @($values)
}

# The ADAS Map grid's "# Items" column renders as "(captured / total)" (e.g.
# "2 / 6"). Confirmed live on RO 2400612505 (Perry, two sibling inspections
# for the same VIN): both share the same total (6, since it's the same
# vehicle spec) but only one has a non-zero *captured* count ("2 / 6" vs.
# "0 / 6") -- total is identical across duplicates and useless as a
# tie-breaker; captured is the actual per-inspection signal. This is a
# provable per-row fact already on screen, not a guess, so it is a safe
# tie-breaker for ambiguous_inspection. Returns $null (not zero) when the
# pattern isn't found at all, so "unknown" and "proven zero" are never
# conflated by a caller checking -gt 0.
function Get-Row-Items-Captured {
    param(
        [System.Windows.Automation.AutomationElement]$Scope
    )

    if ($null -eq $Scope) { return $null }
    foreach ($value in (Visible-Names-In-Scope -Root $Scope)) {
        $match = [regex]::Match([string]$value, '^\(?\s*(\d+)\s*/\s*(\d+)\s*\)?$')
        if ($match.Success) {
            return [int]$match.Groups[1].Value
        }
    }
    return $null
}


function Find-Exact-Ro-Hits {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$Ro
    )

    $found = @()
    if ($null -eq $Root -or [string]::IsNullOrWhiteSpace($Ro)) { return $found }
    $pattern = "(?<!\d)" + [regex]::Escape($Ro.Trim()) + "(?!\d)"
    foreach ($element in @($Root) + @(Descendants -Root $Root)) {
        try {
            if ($element.Current.IsOffscreen) { continue }
            $name = [string]$element.Current.Name
            if (-not [regex]::IsMatch($name, $pattern)) { continue }
            $found += $element
        }
        catch {}
    }
    return @($found)
}


function Inspection-Ids-In-Scope {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$ExcludedRo
    )

    if ($null -eq $Root) { return @() }
    $values = @()
    try {
        $rootName = [string]$Root.Current.Name
        if (-not [string]::IsNullOrWhiteSpace($rootName)) { $values += $rootName }
    }
    catch {}
    foreach ($element in (Descendants -Root $Root)) {
        if ($values.Count -ge 350) { break }
        try {
            if ($element.Current.IsOffscreen) { continue }
            $name = [string]$element.Current.Name
            if (-not [string]::IsNullOrWhiteSpace($name)) { $values += $name }
        }
        catch {}
    }

    $named = @()
    $bare = @()
    foreach ($value in $values) {
        $text = [string]$value
        foreach ($match in [regex]::Matches(
            $text,
            '(?i)\binspection(?:\s*(?:id|number|no\.?|#))?\s*[:#-]?\s*(\d{5,12})\b'
        )) {
            $id = [string]$match.Groups[1].Value
            if ($id -and $id -ne $ExcludedRo) { $named += $id }
        }
        if ($text -match '^\s*(\d{6,9})\s*$') {
            $id = [string]$Matches[1]
            if ($id -and $id -ne $ExcludedRo) { $bare += $id }
        }
    }

    $named = @($named | Select-Object -Unique)
    if ($named.Count -gt 0) { return $named }
    return @($bare | Select-Object -Unique)
}


function Inspection-Row-Association {
    param(
        [System.Windows.Automation.AutomationElement]$Document,
        [System.Windows.Automation.AutomationElement]$VehicleRow,
        [System.Windows.Automation.AutomationElement]$InspectionScope,
        [string]$Ro,
        [string]$Vin,
        [string]$InspectionId
    )

    $association = Deepest-Common-Ancestor `
        -Elements @($VehicleRow, $InspectionScope) `
        -ExcludedRoot $Document
    if ($null -eq $association) {
        return @{ confirmed = $false; reason = "no_shared_row_container" }
    }

    $values = @(Visible-Names-In-Scope -Root $association)
    $roPattern = "(?<!\d)" + [regex]::Escape($Ro.Trim()) + "(?!\d)"
    # The shared ancestor of VehicleRow + InspectionScope always contains
    # VehicleRow's own RO# text, so matching against $values here trivially
    # confirms every inspection under the same vehicle row -- including a
    # second/duplicate inspection whose own RO# column is blank. Confirmed
    # against a live case (RO 2400911760): the ADAS Map grid showed one
    # inspection row with RO# populated and a sibling inspection row where
    # that column was entirely empty, yet both passed this check. Require
    # the RO# to appear specifically within this inspection's own row.
    $ownValues = @(Visible-Names-In-Scope -Root $InspectionScope)
    $roFound = @($ownValues | Where-Object { [regex]::IsMatch([string]$_, $roPattern) }).Count -gt 0

    $vins = @()
    foreach ($value in $values) {
        foreach ($match in [regex]::Matches(
            ([string]$value).ToUpperInvariant(),
            '\b[A-HJ-NPR-Z0-9]{17}\b'
        )) {
            $vins += [string]$match.Value
        }
    }
    $vins = @($vins | Select-Object -Unique)
    $inspectionIds = @(
        Inspection-Ids-In-Scope -Root $association -ExcludedRo $Ro |
        Select-Object -Unique
    )
    $inspectionPattern = "(?<!\d)" + [regex]::Escape($InspectionId) + "(?!\d)"
    $inspectionFound = @(
        $values |
        Where-Object { [regex]::IsMatch([string]$_, $inspectionPattern) }
    ).Count -gt 0
    $confirmed = (
        $roFound -and
        $vins.Count -eq 1 -and
        $vins[0] -eq $Vin -and
        $inspectionFound
    )
    return @{
        confirmed = $confirmed
        reason = if ($confirmed) { $null } else { "association_identity_ambiguous" }
        element = $association
        data = Element-Data -Element $association
        ro_found = $roFound
        vins = $vins
        inspection_found = $inspectionFound
        inspection_ids = $inspectionIds
    }
}


function Resolve-Inspection-View {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [System.Windows.Automation.AutomationElement]$ExactRow,
        [string]$Ro,
        [string]$ExpectedVin,
        [string]$ExpectedInspectionId = ""
    )

    $roHits = @(Find-Exact-Ro-Hits -Root $ExactRow -Ro $Ro)
    if ($roHits.Count -eq 0) {
        return @{ status = "ro_not_visible"; view = $null; inspection_id = $null; candidates = @() }
    }

    $roAncestors = @{}
    foreach ($hit in $roHits) {
        foreach ($node in (Ancestor-Chain -Element $hit -Limit 18)) {
            if (-not $node.key) { continue }
            if (-not $roAncestors.ContainsKey($node.key) -or $node.depth -lt $roAncestors[$node.key].depth) {
                $roAncestors[$node.key] = [pscustomobject]@{
                    depth = $node.depth
                    hit = $hit
                }
            }
        }
    }

    $viewElements = @(Find-View-Candidates -Root $Root)
    if ($viewElements.Count -eq 0) {
        return @{ status = "view_not_found"; view = $null; inspection_id = $null; candidates = @() }
    }

    $candidates = @()
    foreach ($view in $viewElements) {
        $viewData = Element-Data -Element $view
        if ($null -eq $viewData) { continue }
        $chain = @(Ancestor-Chain -Element $view -Limit 18)

        $commonDepth = 999
        foreach ($node in $chain) {
            if ($node.key -and $roAncestors.ContainsKey($node.key)) {
                $totalDepth = [int]$node.depth + [int]$roAncestors[$node.key].depth
                if ($totalDepth -lt $commonDepth) { $commonDepth = $totalDepth }
            }
        }

        $inspectionIds = @()
        $inspectionScope = $null
        foreach ($node in ($chain | Select-Object -First 10)) {
            try {
                $type = $node.element.Current.ControlType
                if (
                    $type -ne [System.Windows.Automation.ControlType]::ListItem -and
                    $type -ne [System.Windows.Automation.ControlType]::DataItem -and
                    $type -ne [System.Windows.Automation.ControlType]::Custom -and
                    $type -ne [System.Windows.Automation.ControlType]::Group
                ) { continue }
            }
            catch { continue }

            $ids = @(Inspection-Ids-In-Scope -Root $node.element -ExcludedRo $Ro)
            if ($ids.Count -eq 1) {
                $inspectionIds = $ids
                $inspectionScope = $node.element
                break
            }
        }

        $verticalDistance = 999999.0
        foreach ($hit in $roHits) {
            try {
                $hitRect = $hit.Current.BoundingRectangle
                $viewRect = $view.Current.BoundingRectangle
                if ($hitRect.Height -gt 0 -and $viewRect.Height -gt 0) {
                    $hitCenter = $hitRect.Top + ($hitRect.Height / 2)
                    $viewCenter = $viewRect.Top + ($viewRect.Height / 2)
                    $distance = [Math]::Abs($viewCenter - $hitCenter)
                    if ($distance -lt $verticalDistance) { $verticalDistance = $distance }
                }
            }
            catch {}
        }

        $controlScore = 0
        if ($viewData.control_type -eq "ControlType.Hyperlink") { $controlScore = 100 }
        elseif ($viewData.control_type -eq "ControlType.Text") { $controlScore = 80 }
        elseif ($viewData.control_type -eq "ControlType.Button") { $controlScore = 70 }
        elseif ($viewData.control_type -eq "ControlType.DataItem") { $controlScore = 60 }
        elseif ($viewData.control_type -eq "ControlType.Custom") { $controlScore = 50 }

        $area = [double]($viewData.rect.width * $viewData.rect.height)
        $association = if ($inspectionIds.Count -eq 1) {
            Inspection-Row-Association `
                -Document $Root `
                -VehicleRow $ExactRow `
                -InspectionScope $inspectionScope `
                -Ro $Ro `
                -Vin $ExpectedVin `
                -InspectionId $inspectionIds[0]
        } else {
            @{ confirmed = $false; reason = "inspection_scope_ambiguous" }
        }
        $candidates += [pscustomobject]@{
            element = $view
            inspection_id = if ($inspectionIds.Count -eq 1) { $inspectionIds[0] } else { $null }
            common_depth = $commonDepth
            vertical_distance = $verticalDistance
            control_score = $controlScore
            area = $area
            data = $viewData
            inspection_scope = Element-Data -Element $inspectionScope
            row_binding_confirmed = [bool]$association.confirmed
            association_scope = $association.data
            association_reason = $association.reason
            items_captured = Get-Row-Items-Captured -Scope $inspectionScope
        }
    }

    $eligible = @(
        $candidates |
        Where-Object { $_.inspection_id -and $_.row_binding_confirmed }
    )
    if (-not [string]::IsNullOrWhiteSpace($ExpectedInspectionId)) {
        $eligible = @($eligible | Where-Object { $_.inspection_id -eq $ExpectedInspectionId })
        if ($eligible.Count -eq 0) {
            return @{
                status = "inspection_mismatch"
                view = $null
                inspection_id = $null
                candidates = $candidates
            }
        }
    }

    if ($eligible.Count -eq 0) {
        return @{
            status = "inspection_id_missing"
            view = $null
            inspection_id = $null
            candidates = $candidates
        }
    }

    $inspectionIds = @($eligible | ForEach-Object { $_.inspection_id } | Select-Object -Unique)
    if ($inspectionIds.Count -ne 1) {
        # Multiple distinct candidate inspections for this vehicle. Before
        # declaring this unresolvable, check the ADAS Map grid's own
        # "# Items" column (Get-Row-Items-Captured) per distinct inspection:
        # if exactly one inspection is proven to have a non-zero *captured*
        # count and every other candidate is proven to be exactly zero,
        # that is a fact already on screen (confirmed live on RO 2400612505,
        # "# Items" column, format "(captured / total)" -- both sibling
        # inspections shared the same total, only captured differed), not a
        # guess, and is a safe tie-breaker. Anything short of that exact
        # pattern (an unknown count for any candidate, or more than one with
        # items captured) still returns ambiguous_inspection -- fail closed,
        # never pick blind between two real candidates.
        $itemsByInspection = @{}
        foreach ($id in $inspectionIds) {
            $totals = @(
                $eligible |
                Where-Object { $_.inspection_id -eq $id -and $null -ne $_.items_captured } |
                ForEach-Object { $_.items_captured } |
                Select-Object -Unique
            )
            if ($totals.Count -eq 1) {
                $itemsByInspection[$id] = $totals[0]
            }
        }
        $withItems = @(
            $inspectionIds |
            Where-Object { $itemsByInspection.ContainsKey($_) -and $itemsByInspection[$_] -gt 0 }
        )
        $withoutItems = @(
            $inspectionIds |
            Where-Object { $itemsByInspection.ContainsKey($_) -and $itemsByInspection[$_] -eq 0 }
        )
        if (
            $withItems.Count -eq 1 -and
            ($withItems.Count + $withoutItems.Count) -eq $inspectionIds.Count
        ) {
            $eligible = @($eligible | Where-Object { $_.inspection_id -eq $withItems[0] })
            $inspectionIds = @($withItems[0])
        }
        else {
            return @{
                status = "ambiguous_inspection"
                view = $null
                inspection_id = $null
                candidates = $candidates
            }
        }
    }

    $best = @(
        $eligible |
        Sort-Object -Property @(
            @{ Expression = "control_score"; Descending = $true },
            @{ Expression = "area"; Ascending = $true },
            @{ Expression = "common_depth"; Ascending = $true },
            @{ Expression = "vertical_distance"; Ascending = $true }
        )
    ) | Select-Object -First 1

    return @{
        status = "resolved"
        view = $best.element
        inspection_id = $best.inspection_id
        row_binding_confirmed = $true
        association_scope = $best.association_scope
        inspection_scope = $best.inspection_scope
        candidates = $candidates
    }
}


function Click-ElementCenter {
    param(
        [System.Windows.Automation.AutomationElement]$Element
    )

    if ($null -eq $Element) {
        return $false
    }

    try {
        $rect = $Element.Current.BoundingRectangle

        if (
            $rect.Width -le 0 -or
            $rect.Height -le 0 -or
            [double]::IsInfinity($rect.Left) -or
            [double]::IsInfinity($rect.Top)
        ) {
            return $false
        }

        $x = [int]($rect.Left + ($rect.Width / 2))
        $y = [int]($rect.Top + ($rect.Height / 2))

        [NativeWindow]::SetCursorPos($x, $y) | Out-Null
        Start-Sleep -Milliseconds 120

        [NativeWindow]::mouse_event(
            $MOUSEEVENTF_LEFTDOWN,
            0,
            0,
            0,
            [UIntPtr]::Zero
        )
        Start-Sleep -Milliseconds 60
        [NativeWindow]::mouse_event(
            $MOUSEEVENTF_LEFTUP,
            0,
            0,
            0,
            [UIntPtr]::Zero
        )

        return $true
    }
    catch {
        return $false
    }
}


function Invoke-InvokePatternOnly {
    param([System.Windows.Automation.AutomationElement]$Element)

    if ($null -eq $Element) { return $false }
    try {
        $invoke = $Element.GetCurrentPattern(
            [System.Windows.Automation.InvokePattern]::Pattern
        )
        if ($null -eq $invoke) { return $false }
        $invoke.Invoke()
        return $true
    }
    catch { return $false }
}


function Invoke-FocusedEnter {
    param([System.Windows.Automation.AutomationElement]$Element)

    if ($null -eq $Element) { return $false }
    try {
        $Element.SetFocus()
        Start-Sleep -Milliseconds 120
        if (-not $Element.Current.HasKeyboardFocus) { return $false }
        [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
        return $true
    }
    catch { return $false }
}


function Invoke-Element {
    param(
        [System.Windows.Automation.AutomationElement]$Element
    )

    try {
        $invoke = $Element.GetCurrentPattern(
            [System.Windows.Automation.InvokePattern]::Pattern
        )
        if ($null -ne $invoke) {
            $invoke.Invoke()
            return $true
        }
    }
    catch {}

    try {
        $selection = $Element.GetCurrentPattern(
            [System.Windows.Automation.SelectionItemPattern]::Pattern
        )
        if ($null -ne $selection) {
            $selection.Select()
            return $true
        }
    }
    catch {}

    try {
        $rect = $Element.Current.BoundingRectangle
        if ($rect.Width -gt 0 -and $rect.Height -gt 0) {
            $x = [int]($rect.Left + ($rect.Width / 2))
            $y = [int]($rect.Top + ($rect.Height / 2))
            [NativeWindow]::SetCursorPos($x, $y) | Out-Null
            [NativeWindow]::mouse_event(
                $MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero
            )
            [NativeWindow]::mouse_event(
                $MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero
            )
            return $true
        }
    }
    catch {}

    return $false
}

function Invoke-Search {
    param([System.Windows.Automation.AutomationElement]$Root)

    $desc = Descendants -Root $Root

    # The search input box is itself named "Search" (automation_id "search",
    # an Edit control) and normally comes before the actual "Search" submit
    # button in document order. Matching by name alone finds the input box
    # first and "clicks" it (a no-op re-focus that still reports success via
    # Invoke-Element's physical-click fallback) -- it never presses Search.
    # Require the real button control type so the submit actually happens.
    foreach ($element in $desc) {
        try {
            if ($element.Current.IsOffscreen -or -not $element.Current.IsEnabled) {
                continue
            }
            if ($element.Current.ControlType -ne [System.Windows.Automation.ControlType]::Button) {
                continue
            }

            $name = [string]$element.Current.Name
            if ($name -notmatch "(?i)^\s*Search\s*$") {
                continue
            }

            if (Invoke-Element -Element $element) {
                return @{
                    invoked = $true
                    control = Element-Data -Element $element
                }
            }
        }
        catch {}
    }

    try {
        [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
        return @{
            invoked = $true
            via = "enter_key"
        }
    }
    catch {}

    return @{ invoked = $false }
}

function Find-Named-Elements {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$Needle,
        [int]$Limit = 50
    )

    $matches = @()
    $desc = Descendants -Root $Root

    foreach ($element in $desc) {
        if ($matches.Count -ge $Limit) { break }

        try {
            $name = [string]$element.Current.Name
            if ([string]::IsNullOrWhiteSpace($name)) {
                continue
            }

            if ($name -notlike "*$Needle*") {
                continue
            }

            $data = Element-Data -Element $element
            if ($null -ne $data) {
                $matches += [pscustomobject]@{
                    element = $element
                    data = $data
                }
            }
        }
        catch {}
    }

    return @($matches)
}

function Find-Row-Ancestor {
    param(
        [System.Windows.Automation.AutomationElement]$Element
    )

    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $cursor = $Element

    for ($i = 0; $i -lt 12; $i++) {
        if ($null -eq $cursor) { break }

        try {
            $type = $cursor.Current.ControlType
            $name = [string]$cursor.Current.Name

            if (
                $type -eq [System.Windows.Automation.ControlType]::DataItem -or
                $type -eq [System.Windows.Automation.ControlType]::ListItem -or
                $type -eq [System.Windows.Automation.ControlType]::Custom
            ) {
                # Prefer an ancestor with more than just the RO number.
                $desc = Descendants -Root $cursor
                $names = @()
                foreach ($child in $desc) {
                    try {
                        $childName = [string]$child.Current.Name
                        if (-not [string]::IsNullOrWhiteSpace($childName)) {
                            $names += $childName
                        }
                    }
                    catch {}
                }

                if ($names.Count -ge 2) {
                    return $cursor
                }
            }

            $cursor = $walker.GetParent($cursor)
        }
        catch {
            break
        }
    }

    return $null
}

function Row-Snapshot {
    param(
        [System.Windows.Automation.AutomationElement]$Row
    )

    if ($null -eq $Row) {
        return $null
    }

    $values = @()
    $controls = @()

    try {
        $name = [string]$Row.Current.Name
        if (-not [string]::IsNullOrWhiteSpace($name)) {
            $values += $name
        }
    }
    catch {}

    $desc = Descendants -Root $Row

    foreach ($element in $desc) {
        try {
            if ($element.Current.IsOffscreen) { continue }

            $name = [string]$element.Current.Name
            if (-not [string]::IsNullOrWhiteSpace($name)) {
                $values += $name
            }

            $data = Element-Data -Element $element
            if ($null -ne $data) {
                $controls += [pscustomobject]@{
                    element = $element
                    data = $data
                }
            }
        }
        catch {}
    }

    $unique = @($values | Where-Object { $_ } | Select-Object -Unique)

    $vin = $null
    foreach ($value in $unique) {
        $match = [regex]::Match(
            $value.ToUpperInvariant(),
            '\b[A-HJ-NPR-Z0-9]{17}\b'
        )
        if ($match.Success) {
            $vin = $match.Value
            break
        }
    }

    return @{
        values = $unique
        vin = $vin
        controls = $controls
    }
}


function Find-Visible-ExactName {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$Name
    )

    $desc = Descendants -Root $Root
    foreach ($element in $desc) {
        try {
            if ($element.Current.IsOffscreen -or -not $element.Current.IsEnabled) {
                continue
            }
            if ([string]$element.Current.Name -match ("(?i)^\s*" + [regex]::Escape($Name) + "\s*$")) {
                return $element
            }
        }
        catch {}
    }
    return $null
}

function Find-Visible-ExactName-CaseSensitive {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$Name
    )

    # The ADAS Map detail toolbar has a capital-case "Report" button, distinct
    # from a lower-case "report" quick-action link elsewhere on the same page.
    # Find-Visible-ExactName is case-insensitive and can pick the wrong one,
    # so this variant matches case exactly.
    $desc = Descendants -Root $Root
    foreach ($element in $desc) {
        try {
            if ($element.Current.IsOffscreen -or -not $element.Current.IsEnabled) {
                continue
            }
            if ([string]$element.Current.Name -cmatch ("^\s*" + [regex]::Escape($Name) + "\s*$")) {
                return $element
            }
        }
        catch {}
    }
    return $null
}

function Close-Inspection-Modal {
    param(
        [System.Windows.Automation.AutomationElement]$Root
    )

    # Deliberately targets the modal's own "x" icon by its stable class
    # name, never a generically-named "Close" button: the ADAS toolbar has
    # its own unrelated "Close" button, and Chrome's tab strip has one
    # "Close" per open tab -- matching by name alone risks closing a
    # browser tab instead of the modal (this happened once; do not repeat
    # it). custom-close is specific to the inspection modal's dismiss icon.
    $desc = Descendants -Root $Root
    $closeIcon = $desc | Where-Object {
        -not $_.Current.IsOffscreen -and
        [string]$_.Current.ClassName -eq "custom-close"
    } | Select-Object -First 1

    if ($null -eq $closeIcon) {
        return @{ closed = $false; reason = "close_icon_not_found" }
    }

    # Physical click first: LegacyIAccessible.DoDefaultAction() on this icon
    # (and on several other Views/Vue-drawn controls in this app) has been
    # observed to report success without producing any real click, which
    # left a stale inspection modal open and once caused a report to be
    # captured for the wrong RO. Verify the icon is actually gone afterward
    # rather than trusting either method's own success flag.
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        if ($attempt -gt 0) { Start-Sleep -Milliseconds 400 }

        Click-ElementCenter -Element $closeIcon | Out-Null
        Start-Sleep -Milliseconds 400

        $stillOpen = @(
            Descendants -Root $Root |
            Where-Object {
                -not $_.Current.IsOffscreen -and
                [string]$_.Current.ClassName -eq "custom-close"
            }
        )

        if ($stillOpen.Count -eq 0) {
            return @{ closed = $true; attempts = ($attempt + 1) }
        }

        $closeIcon = $stillOpen[0]
    }

    Invoke-LegacyDefaultAction -Element $closeIcon | Out-Null
    Start-Sleep -Milliseconds 400
    $stillOpenFinal = @(
        Descendants -Root $Root |
        Where-Object {
            -not $_.Current.IsOffscreen -and
            [string]$_.Current.ClassName -eq "custom-close"
        }
    )
    $clicked = ($stillOpenFinal.Count -eq 0)

    return @{ closed = $clicked }
}

function Collect-Visible-Names {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [int]$Limit = 1200
    )

    $values = @()
    $desc = Descendants -Root $Root

    foreach ($element in $desc) {
        if ($values.Count -ge $Limit) { break }
        try {
            if ($element.Current.IsOffscreen) { continue }
            $name = [string]$element.Current.Name
            if ([string]::IsNullOrWhiteSpace($name)) { continue }
            $values += $name
        }
        catch {}
    }

    return @($values | Select-Object -Unique)
}

function Extract-Vin-From-Values {
    param($Values)

    foreach ($value in $Values) {
        $match = [regex]::Match(
            [string]$value.ToUpperInvariant(),
            '\b[A-HJ-NPR-Z0-9]{17}\b'
        )
        if ($match.Success) {
            return $match.Value
        }
    }
    return $null
}

function Element-Link-Value {
    param([System.Windows.Automation.AutomationElement]$Element)

    if ($null -eq $Element) { return $null }
    try {
        $valuePattern = $Element.GetCurrentPattern(
            [System.Windows.Automation.ValuePattern]::Pattern
        )
        if ($null -ne $valuePattern) {
            $value = [string]$valuePattern.Current.Value
            if ($value -match '^https?://') { return $value }
        }
    }
    catch {}
    try {
        $legacy = $Element.GetCurrentPattern(
            [System.Windows.Automation.LegacyIAccessiblePattern]::Pattern
        )
        if ($null -ne $legacy) {
            $value = [string]$legacy.Current.Value
            if ($value -match '^https?://') { return $value }
        }
    }
    catch {}
    try {
        $help = [string]$Element.Current.HelpText
        $match = [regex]::Match($help, 'https?://[^\s]+' )
        if ($match.Success) { return $match.Value }
    }
    catch {}
    return $null
}


function Test-AdasMap-Url {
    param(
        [string]$Value,
        [switch]$OpusOnly
    )

    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    try {
        $uri = [Uri]$Value
        if ($uri.Scheme -ne "https") { return $false }
        $hostName = $uri.DnsSafeHost.ToLowerInvariant()
        if ($OpusOnly) { return $hostName -eq "opus.adasmap.com" }
        return ($hostName -eq "adasmap.com" -or $hostName.EndsWith(".adasmap.com"))
    }
    catch { return $false }
}


function Chrome-Address-AdasMap-Url {
    param([System.Windows.Automation.AutomationElement]$ChromeRoot)

    if ($null -eq $ChromeRoot) { return $null }
    foreach ($element in (Descendants -Root $ChromeRoot)) {
        try {
            if (
                $element.Current.IsOffscreen -or
                $element.Current.ControlType -ne [System.Windows.Automation.ControlType]::Edit
            ) { continue }
            $name = [string]$element.Current.Name
            $automationId = [string]$element.Current.AutomationId
            if (
                $name -notmatch '(?i)address and search bar' -and
                $automationId -notmatch '(?i)address'
            ) { continue }
            $value = Element-Link-Value -Element $element
            # Chrome-root access is metadata-only and intentionally narrower
            # than Document URL discovery. It never feeds page content parsing.
            if (Test-AdasMap-Url -Value $value -OpusOnly) { return $value }
        }
        catch {}
    }
    return $null
}


function Document-Source-Url {
    param(
        [System.Windows.Automation.AutomationElement]$Document,
        [System.Windows.Automation.AutomationElement]$ChromeRoot = $null
    )

    $value = Element-Link-Value -Element $Document
    if (Test-AdasMap-Url -Value $value) { return $value }
    try {
        $name = [string]$Document.Current.Name
        $match = [regex]::Match($name, 'https?://[^\s]+' )
        if ($match.Success -and (Test-AdasMap-Url -Value $match.Value)) { return $match.Value }
    }
    catch {}
    return Chrome-Address-AdasMap-Url -ChromeRoot $ChromeRoot
}


function Observable-Detail-Links {
    param([System.Windows.Automation.AutomationElement]$Root)

    $alldata = @()
    $reports = @()
    foreach ($element in (Descendants -Root $Root)) {
        try {
            if ($element.Current.IsOffscreen) { continue }
            $type = $element.Current.ControlType
            $className = [string]$element.Current.ClassName
            if (
                $type -ne [System.Windows.Automation.ControlType]::Hyperlink -and
                $className -notmatch '(?i)custom-link'
            ) { continue }

            $url = Element-Link-Value -Element $element
            if (-not $url) { continue }
            $name = [string]$element.Current.Name
            $urlFolded = $url.ToLowerInvariant()
            if ($urlFolded -match 'alldata') { $alldata += $url }
            if ($urlFolded -match '(report|download|print|\.pdf(?:\b|\?))') { $reports += $url }
        }
        catch {}
    }
    return @{
        alldata_links = @($alldata | Select-Object -Unique)
        report_links = @($reports | Select-Object -Unique)
    }
}


function Required-Tab-State {
    param([System.Windows.Automation.AutomationElement]$Root)

    $buttons = @(
        Descendants -Root $Root |
        Where-Object {
            try {
                -not $_.Current.IsOffscreen -and
                $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and
                [string]$_.Current.ClassName -match '(?i)^nav-link\s+btn\s+border\s+btn-(?:success|link)$'
            }
            catch { $false }
        }
    )
    $required = @($buttons | Where-Object { [string]$_.Current.Name -match '(?i)^\s*Required\s*$' })
    $notRequired = @($buttons | Where-Object { [string]$_.Current.Name -match '(?i)^\s*Not Required\s*$' })
    $notInstalled = @($buttons | Where-Object { [string]$_.Current.Name -match '(?i)^\s*Not Installed\s*$' })

    $structureConfirmed = (
        $required.Count -eq 1 -and
        $notRequired.Count -eq 1 -and
        $notInstalled.Count -eq 1
    )
    $sameRow = $false
    if ($structureConfirmed) {
        try {
            $top = $required[0].Current.BoundingRectangle.Top
            $sameRow = (
                [Math]::Abs($notRequired[0].Current.BoundingRectangle.Top - $top) -le 4 -and
                [Math]::Abs($notInstalled[0].Current.BoundingRectangle.Top - $top) -le 4
            )
        }
        catch {}
    }

    $requiredSelected = $false
    if ($structureConfirmed -and $sameRow) {
        try {
            $requiredClass = [string]$required[0].Current.ClassName
            $requiredSelected = (
                $requiredClass -match '(?i)\bbtn-success\b' -and
                -not $required[0].Current.IsEnabled
            )
        }
        catch {}
    }

    return @{
        structure_confirmed = ($structureConfirmed -and $sameRow)
        required_selected = $requiredSelected
        required = if ($required.Count -eq 1) { $required[0] } else { $null }
        not_required = if ($notRequired.Count -eq 1) { $notRequired[0] } else { $null }
        not_installed = if ($notInstalled.Count -eq 1) { $notInstalled[0] } else { $null }
        controls = @($buttons | ForEach-Object { Element-Data -Element $_ })
    }
}


function Resolve-Detail-Modal {
    param(
        [System.Windows.Automation.AutomationElement]$Document,
        [string]$ExpectedInspectionId
    )

    if ($null -eq $Document -or [string]::IsNullOrWhiteSpace($ExpectedInspectionId)) {
        return @{ confirmed = $false; status = "detail_modal_identity_missing" }
    }
    $desc = @(Descendants -Root $Document)
    $closeIcons = @(
        $desc |
        Where-Object {
            try {
                -not $_.Current.IsOffscreen -and
                [string]$_.Current.ClassName -eq "custom-close"
            }
            catch { $false }
        }
    )
    $tabState = Required-Tab-State -Root $Document
    if (
        $closeIcons.Count -eq 0 -or
        -not $tabState.structure_confirmed
    ) {
        return @{ confirmed = $false; status = "detail_modal_markers_missing" }
    }

    $reports = @(
        $desc |
        Where-Object {
            try {
                -not $_.Current.IsOffscreen -and
                [string]$_.Current.Name -cmatch '^\s*Report\s*$'
            }
            catch { $false }
        }
    )
    $inspectionPattern = "(?<!\d)" + [regex]::Escape($ExpectedInspectionId) + "(?!\d)"
    $inspectionElements = @(
        $desc |
        Where-Object {
            try {
                -not $_.Current.IsOffscreen -and
                [regex]::IsMatch([string]$_.Current.Name, $inspectionPattern)
            }
            catch { $false }
        }
    )
    if ($reports.Count -eq 0 -or $inspectionElements.Count -eq 0) {
        return @{ confirmed = $false; status = "detail_modal_identity_missing" }
    }

    $byRoot = @{}
    foreach ($closeIcon in $closeIcons) {
        foreach ($report in $reports) {
            foreach ($inspectionElement in $inspectionElements) {
                $markers = @(
                    $closeIcon,
                    $tabState.required,
                    $tabState.not_required,
                    $tabState.not_installed,
                    $report,
                    $inspectionElement
                )
                $common = Deepest-Common-Ancestor `
                    -Elements $markers `
                    -ExcludedRoot $Document
                if ($null -eq $common) { continue }

                # If the inspection ID is only in the background vehicle grid,
                # the nearest common ancestor is the page/app shell and still
                # contains a visible View control. A real modal subtree does not.
                if (@(Find-View-Candidates -Root $common).Count -gt 0) { continue }

                try {
                    $commonRect = $common.Current.BoundingRectangle
                    if ($commonRect.Width -le 0 -or $commonRect.Height -le 0) { continue }
                    $containsMarkers = $true
                    foreach ($marker in $markers) {
                        $rect = $marker.Current.BoundingRectangle
                        $centerX = $rect.Left + ($rect.Width / 2)
                        $centerY = $rect.Top + ($rect.Height / 2)
                        if (
                            $centerX -lt ($commonRect.Left - 3) -or
                            $centerX -gt ($commonRect.Left + $commonRect.Width + 3) -or
                            $centerY -lt ($commonRect.Top - 3) -or
                            $centerY -gt ($commonRect.Top + $commonRect.Height + 3)
                        ) {
                            $containsMarkers = $false
                            break
                        }
                    }
                    if (-not $containsMarkers) { continue }
                }
                catch { continue }

                $rootKey = Element-Key -Element $common
                if (-not $rootKey) { continue }
                if (-not $byRoot.ContainsKey($rootKey)) {
                    $byRoot[$rootKey] = @{
                        root = $common
                        root_data = Element-Data -Element $common
                        close = $closeIcon
                        report = $report
                        inspection = $inspectionElement
                    }
                }
            }
        }
    }

    if ($byRoot.Count -eq 0) {
        return @{ confirmed = $false; status = "detail_modal_not_proven" }
    }
    if ($byRoot.Count -ne 1) {
        return @{ confirmed = $false; status = "ambiguous_detail_modal" }
    }
    $resolved = @($byRoot.Values)[0]
    return @{
        confirmed = $true
        status = "detail_modal_resolved"
        root = $resolved.root
        root_data = $resolved.root_data
        close = $resolved.close
        report = $resolved.report
        inspection = $resolved.inspection
    }
}


function Ensure-Required-Tab {
    param([System.Windows.Automation.AutomationElement]$Document)

    $state = Required-Tab-State -Root $Document
    if (-not $state.structure_confirmed) {
        return @{ confirmed = $false; activated = $false; reason = "required_tab_structure_ambiguous"; state = $state }
    }
    if ($state.required_selected) {
        return @{ confirmed = $true; activated = $false; state = $state }
    }

    $activated = Invoke-InvokePatternOnly -Element $state.required
    if (-not $activated) { $activated = Invoke-LegacyDefaultAction -Element $state.required }
    if (-not $activated) { $activated = Click-ElementCenter -Element $state.required }
    if (-not $activated) {
        return @{ confirmed = $false; activated = $false; reason = "required_tab_activation_failed"; state = $state }
    }

    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        Start-Sleep -Milliseconds 250
        $state = Required-Tab-State -Root $Document
        if ($state.structure_confirmed -and $state.required_selected) {
            return @{ confirmed = $true; activated = $true; state = $state }
        }
    }
    return @{ confirmed = $false; activated = $true; reason = "required_tab_selection_unconfirmed"; state = $state }
}


function Requirement-Records {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        $RequiredTabState,
        [System.Windows.Automation.AutomationElement]$ReportButton,
        [string]$ModalRuntimeId
    )

    # Live ADAS Map detail rows are exposed as ListItem controls. The
    # authoritative requirement name is the FIRST descendant with the portal's
    # custom-link class. Parsing that structure (rather than keyword-scanning
    # all page names) excludes browser chrome, page actions, and explanatory
    # radar/camera prose.
    if (
        $null -eq $RequiredTabState -or
        -not $RequiredTabState.structure_confirmed -or
        -not $RequiredTabState.required_selected
    ) { return @() }

    if ($null -eq $ReportButton -or [string]::IsNullOrWhiteSpace($ModalRuntimeId)) { return @() }

    try {
        $requiredRect = $RequiredTabState.required.Current.BoundingRectangle
        $reportRect = $ReportButton.Current.BoundingRectangle
        $modalRect = $Root.Current.BoundingRectangle
        $regionTop = $requiredRect.Top + $requiredRect.Height
        $regionBottom = $reportRect.Top
    }
    catch { return @() }
    if ($regionBottom -le $regionTop) { return @() }

    $records = @()
    $seen = @{}
    $listItems = @(
        Descendants -Root $Root |
        Where-Object {
            try {
                -not $_.Current.IsOffscreen -and
                $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::ListItem
            }
            catch { $false }
        }
    )

    foreach ($item in $listItems) {
        try {
            $itemRect = $item.Current.BoundingRectangle
            if (
                $itemRect.Height -le 0 -or
                $itemRect.Top -lt ($regionTop - 2) -or
                ($itemRect.Top + $itemRect.Height) -gt ($regionBottom + 2) -or
                $itemRect.Left -lt ($modalRect.Left - 3) -or
                ($itemRect.Left + $itemRect.Width) -gt ($modalRect.Left + $modalRect.Width + 3)
            ) { continue }
        }
        catch { continue }

        $rowDesc = @(Descendants -Root $item)
        $customLinks = @(
            $rowDesc |
            Where-Object {
                try {
                    -not $_.Current.IsOffscreen -and
                    [string]$_.Current.ClassName -match '(?i)(?:^|\s)custom-link(?:\s|$)'
                }
                catch { $false }
            }
        )
        if ($customLinks.Count -eq 0) { continue }

        $requirementControl = $customLinks[0]
        $label = ""
        try { $label = (([string]$requirementControl.Current.Name -split '\s+' | Where-Object { $_ }) -join " ").Trim() } catch {}
        if ([string]::IsNullOrWhiteSpace($label) -or $label.Length -gt 240) { continue }

        # These are modal/page operations, never calibration requirements.
        if ($label -match '(?i)^\s*(view|report|download|print|close|edit|delete|add|save|cancel|required|recommended|all inspections?)\s*$') {
            continue
        }

        $key = ($label.ToLowerInvariant() -replace '[^a-z0-9]+', '')
        if (-not $key -or $seen.ContainsKey($key)) { continue }
        $seen[$key] = $true

        $rowValues = @()
        foreach ($element in $rowDesc) {
            try {
                if ($element.Current.IsOffscreen) { continue }
                $name = [string]$element.Current.Name
                if (-not [string]::IsNullOrWhiteSpace($name)) { $rowValues += $name }
            }
            catch {}
        }

        $records += [pscustomobject]@{
            label = $label
            source = "adas_map_required_list_item"
            source_context = "selected_required_modal"
            source_context_runtime_id = $ModalRuntimeId
            source_control_class = [string]$requirementControl.Current.ClassName
            row_values = @($rowValues | Select-Object -Unique)
            link = Element-Link-Value -Element $requirementControl
        }
    }

    return @($records)
}


function Detail-State {
    param(
        [System.Windows.Automation.AutomationElement]$Document,
        [string]$ExpectedInspectionId,
        [System.Windows.Automation.AutomationElement]$ChromeRoot = $null
    )

    $snapshot = Document-Snapshot -Document $Document
    $modal = Resolve-Detail-Modal `
        -Document $Document `
        -ExpectedInspectionId $ExpectedInspectionId
    if (-not $modal.confirmed) {
        return @{
            confirmed = $false
            modal_inspection_confirmed = $false
            modal_status = $modal.status
            inspection_id_found = $false
            modal_close_found = $false
            report_button_found = $false
            required_context_confirmed = $false
            required_region_confirmed = $false
            required_tab_controls = @()
            requirements_parse_confident = $false
            explicit_no_calibration = $false
            requirements = @()
            source_url = Document-Source-Url -Document $Document -ChromeRoot $ChromeRoot
            report_links = @()
            alldata_links = @()
            snapshot = $snapshot
        }
    }

    $modalRoot = $modal.root
    $modalRuntimeId = [string]$modal.root_data.runtime_id
    $desc = @(Descendants -Root $modalRoot)
    $reportButton = $modal.report
    $requiredTabState = Required-Tab-State -Root $modalRoot
    $requirements = @(
        Requirement-Records `
            -Root $modalRoot `
            -RequiredTabState $requiredTabState `
            -ReportButton $reportButton `
            -ModalRuntimeId $modalRuntimeId
    )
    $explicitNoCalibration = $false
    if (
        $requiredTabState.structure_confirmed -and
        $requiredTabState.required_selected -and
        $null -ne $reportButton
    ) {
        try {
            $requiredRect = $requiredTabState.required.Current.BoundingRectangle
            $modalRect = $modalRoot.Current.BoundingRectangle
            $regionTop = $requiredRect.Top + $requiredRect.Height
            $regionBottom = $reportButton.Current.BoundingRectangle.Top
            foreach ($element in $desc) {
                if ($element.Current.IsOffscreen) { continue }
                $rect = $element.Current.BoundingRectangle
                if (
                    $rect.Top -lt ($regionTop - 2) -or
                    ($rect.Top + $rect.Height) -gt ($regionBottom + 2) -or
                    $rect.Left -lt ($modalRect.Left - 3) -or
                    ($rect.Left + $rect.Width) -gt ($modalRect.Left + $modalRect.Width + 3)
                ) { continue }
                $value = [string]$element.Current.Name
                if ($value -match '(?i)^\s*no\s+calibrations?\s+(?:are\s+)?required\s*$') {
                    $explicitNoCalibration = $true
                    break
                }
            }
        }
        catch {}
    }

    $requiredRegionConfirmed = (
        $requiredTabState.structure_confirmed -and
        $requiredTabState.required_selected
    )
    $parseConfident = (
        $requiredRegionConfirmed -and
        ($requirements.Count -gt 0 -or $explicitNoCalibration)
    )
    $links = Observable-Detail-Links -Root $modalRoot

    return @{
        confirmed = $true
        modal_inspection_confirmed = $true
        modal_status = $modal.status
        modal_runtime_id = $modalRuntimeId
        inspection_id_found = $true
        modal_close_found = $true
        report_button_found = ($null -ne $reportButton)
        required_context_confirmed = $requiredRegionConfirmed
        required_region_confirmed = $requiredRegionConfirmed
        required_tab_controls = $requiredTabState.controls
        requirements_parse_confident = $parseConfident
        explicit_no_calibration = $explicitNoCalibration
        requirements = $requirements
        source_url = Document-Source-Url -Document $Document -ChromeRoot $ChromeRoot
        report_links = $links.report_links
        alldata_links = $links.alldata_links
        snapshot = $snapshot
    }
}


function Vehicle-From-Row-Values {
    param(
        $Values,
        [string]$Vin
    )

    $items = @($Values | ForEach-Object { ([string]$_).Trim() })
    $vinIndex = -1
    for ($i = 0; $i -lt $items.Count; $i++) {
        if ($Vin -and $items[$i] -eq $Vin) {
            $vinIndex = $i
            break
        }
    }
    if ($vinIndex -lt 0) { return $null }

    $yearIndex = -1
    for ($i = $vinIndex + 1; $i -lt $items.Count; $i++) {
        if ($items[$i] -match '^(19|20)\d{2}$') {
            $yearIndex = $i
            break
        }
    }
    if ($yearIndex -lt 0 -or ($yearIndex + 2) -ge $items.Count) { return $null }

    $make = $items[$yearIndex + 1]
    $modelConfiguration = $items[$yearIndex + 2]
    if (
        [string]::IsNullOrWhiteSpace($make) -or
        [string]::IsNullOrWhiteSpace($modelConfiguration) -or
        $make -match '^\d+$' -or
        $modelConfiguration -match '^\d{1,2}/\d{1,2}/\d{4}'
    ) { return $null }

    return @{
        year = [int]$items[$yearIndex]
        make = $make
        model_configuration = $modelConfiguration
        observed_label = ($items[$yearIndex] + " " + $make + " " + $modelConfiguration)
    }
}


function Test-Vehicle-Hints {
    param(
        $Vehicle,
        [int]$Year = 0,
        [string]$Make = "",
        [string]$Model = ""
    )

    if ($null -eq $Vehicle) { return $false }
    if ($Year -le 0 -or [string]::IsNullOrWhiteSpace($Make) -or [string]::IsNullOrWhiteSpace($Model)) {
        # Partial CIQ identity is not strong enough to choose between two
        # authoritative ADAS Map rows that share the same RO number.
        return $false
    }

    $observedMake = (([string]$Vehicle.make -split '\s+' | Where-Object { $_ }) -join " ").Trim()
    $expectedMake = (($Make -split '\s+' | Where-Object { $_ }) -join " ").Trim()
    if ($observedMake -ine $expectedMake -or [int]$Vehicle.year -ne $Year) { return $false }

    $observedModel = (([string]$Vehicle.model_configuration -split '\s+' | Where-Object { $_ }) -join " ").Trim()
    $expectedModel = (($Model -split '\s+' | Where-Object { $_ }) -join " ").Trim()
    $truncated = ($expectedModel -match '(?:\.\.\.|…)$')
    if ($truncated) {
        $expectedModel = ($expectedModel -replace '(?:\.\.\.|…)$', '').Trim()
        if ([string]::IsNullOrWhiteSpace($expectedModel)) { return $false }
        return (
            $observedModel -ieq $expectedModel -or
            $observedModel.StartsWith($expectedModel + " ", [System.StringComparison]::OrdinalIgnoreCase)
        )
    }

    # A nontruncated hint must match the entire combined portal model field.
    # Prefix-only matching here could silently choose a trim/configuration that
    # CIQ did not actually identify.
    return $observedModel -ieq $expectedModel
}


function Read-Current-Ro {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$Ro,
        [string]$ExpectedInspectionId = "",
        [int]$ExpectedVehicleYear = 0,
        [string]$ExpectedVehicleMake = "",
        [string]$ExpectedVehicleModel = ""
    )

    $hits = @(Find-Exact-Ro-Hits -Root $Root -Ro $Ro)

    if ($hits.Count -eq 0) {
        # Every key present here on purpose: a Hashtable with no explicit
        # "values"/"vin"/"view_available" key falls back to .NET's own
        # case-insensitively-matched Hashtable.Values property on access
        # (e.g. $result.values), which silently returns the wrong thing
        # (every stored value, not the intended field) instead of $null.
        return @{
            found = $false
            ro_number = $Ro
            hit_count = 0
            vin = $null
            vehicle = $null
            values = @()
            view_available = $false
            inspection_id = $null
            row_binding_confirmed = $false
            resolution_status = "ro_not_visible"
            view_candidates = @()
        }
    }

    # Collapse repeated accessible text nodes to their real row container.
    # More than one complete row for the same exact RO is not safe to rank.
    $rowCandidates = @{}
    foreach ($hit in $hits) {
        $row = Find-Row-Ancestor -Element $hit
        $rowKey = Element-Key -Element $row
        if (-not $rowKey -or $rowCandidates.ContainsKey($rowKey)) { continue }
        $snap = Row-Snapshot -Row $row
        if ($null -eq $snap -or -not $snap.vin) { continue }
        $vehicle = Vehicle-From-Row-Values -Values $snap.values -Vin $snap.vin
        if ($null -eq $vehicle) { continue }
        $rowCandidates[$rowKey] = @{
            hit = $hit
            row = $row
            snapshot = $snap
            vehicle = $vehicle
        }
    }

    if ($rowCandidates.Count -eq 0) {
        return @{
            found = $true
            ro_number = $Ro
            hit_count = $hits.Count
            vin = $null
            vehicle = $null
            values = @($hits | ForEach-Object { [string]$_.Current.Name })
            view_available = $false
            inspection_id = $null
            row_binding_confirmed = $false
            resolution_status = "vehicle_identity_unparsed"
            view_candidates = @()
        }
    }
    $candidateRows = @($rowCandidates.Values)
    $vehicleHintApplied = $false
    if ($candidateRows.Count -gt 1) {
        $completeHint = (
            $ExpectedVehicleYear -gt 0 -and
            -not [string]::IsNullOrWhiteSpace($ExpectedVehicleMake) -and
            -not [string]::IsNullOrWhiteSpace($ExpectedVehicleModel)
        )
        if ($completeHint) {
            $vehicleHintApplied = $true
            $candidateRows = @(
                $candidateRows |
                Where-Object {
                    Test-Vehicle-Hints `
                        -Vehicle $_.vehicle `
                        -Year $ExpectedVehicleYear `
                        -Make $ExpectedVehicleMake `
                        -Model $ExpectedVehicleModel
                }
            )
            if ($candidateRows.Count -eq 0) {
                return @{
                    found = $true
                    ro_number = $Ro
                    hit_count = $hits.Count
                    vin = $null
                    vehicle = $null
                    values = @()
                    view_available = $false
                    inspection_id = $null
                    row_binding_confirmed = $false
                    resolution_status = "vehicle_identity_mismatch"
                    vehicle_hint_applied = $true
                    candidate_vehicles = @($rowCandidates.Values | ForEach-Object { $_.vehicle })
                    view_candidates = @()
                }
            }
        }
    }
    if ($candidateRows.Count -ne 1) {
        return @{
            found = $true
            ro_number = $Ro
            hit_count = $hits.Count
            vin = $null
            vehicle = $null
            values = @()
            view_available = $false
            inspection_id = $null
            row_binding_confirmed = $false
            resolution_status = "ambiguous_ro"
            vehicle_hint_applied = $vehicleHintApplied
            candidate_vehicles = @($candidateRows | ForEach-Object { $_.vehicle })
            view_candidates = @()
        }
    }
    $best = $candidateRows[0]

    # Resolve View by exact RO ancestry and a proven inspection-row ID. Never
    # fall back to the first global View: multiple expanded rows can coexist.
    $resolution = Resolve-Inspection-View `
        -Root $Root `
        -ExactRow $best.row `
        -Ro $Ro `
        -ExpectedVin $best.snapshot.vin `
        -ExpectedInspectionId $ExpectedInspectionId
    $view = $resolution.view
    $candidateDiagnostics = @(
        $resolution.candidates |
        ForEach-Object {
            [pscustomobject]@{
                inspection_id = $_.inspection_id
                common_depth = $_.common_depth
                vertical_distance = $_.vertical_distance
                control_score = $_.control_score
                area = $_.area
                control = $_.data
                inspection_scope = $_.inspection_scope
                row_binding_confirmed = $_.row_binding_confirmed
                association_scope = $_.association_scope
                association_reason = $_.association_reason
                items_captured = $_.items_captured
            }
        }
    )
    $rowVin = if ($best.snapshot) { $best.snapshot.vin } else { $null }
    $vehicle = $best.vehicle

    return @{
        found = $true
        ro_number = $Ro
        hit_count = $hits.Count
        vin = $rowVin
        vehicle = $vehicle
        values = if ($best.snapshot) { $best.snapshot.values } else { @() }
        view_available = ($null -ne $view)
        row_element = $best.row
        view_element = $view
        inspection_id = $resolution.inspection_id
        row_binding_confirmed = [bool]$resolution.row_binding_confirmed
        row_scope = Element-Data -Element $best.row
        association_scope = $resolution.association_scope
        inspection_scope = $resolution.inspection_scope
        vehicle_hint_applied = $vehicleHintApplied
        resolution_status = $resolution.status
        view_candidates = $candidateDiagnostics
    }
}


function Resolve-Exact-Row-EditLeaf {
    param([System.Windows.Automation.AutomationElement]$Row)

    if ($null -eq $Row) {
        return @{ status = "target_row_missing"; element = $null; candidates = @() }
    }
    $candidates = @(
        Descendants -Root $Row |
        Where-Object {
            try {
                -not $_.Current.IsOffscreen -and
                $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Hyperlink -and
                [string]$_.Current.Name -match '(?i)^\s*edit\s*$'
            }
            catch { $false }
        }
    )
    if ($candidates.Count -eq 0) {
        return @{ status = "row_edit_not_found"; element = $null; candidates = @() }
    }
    if ($candidates.Count -ne 1) {
        return @{
            status = "row_edit_ambiguous"
            element = $null
            candidates = @($candidates | ForEach-Object { Element-Data -Element $_ })
        }
    }
    return @{
        status = "row_edit_resolved"
        element = $candidates[0]
        candidates = @(Element-Data -Element $candidates[0])
    }
}


function Test-Same-Observed-Vehicle-Row {
    param($ExpectedCurrent, $ActualCurrent)

    if (
        $null -eq $ExpectedCurrent -or
        $null -eq $ActualCurrent -or
        -not $ActualCurrent.found -or
        -not $ActualCurrent.vehicle_hint_applied -or
        [string]$ActualCurrent.vin -ne [string]$ExpectedCurrent.vin
    ) { return $false }
    return (
        [int]$ActualCurrent.vehicle.year -eq [int]$ExpectedCurrent.vehicle.year -and
        [string]$ActualCurrent.vehicle.make -ieq [string]$ExpectedCurrent.vehicle.make -and
        [string]$ActualCurrent.vehicle.model_configuration -ieq [string]$ExpectedCurrent.vehicle.model_configuration
    )
}


function Expand-Proven-Vehicle-Row {
    param(
        [System.Windows.Automation.AutomationElement]$ChromeRoot,
        [string]$Ro,
        $InitialCurrent,
        [string]$ExpectedInspectionId = "",
        [int]$ExpectedVehicleYear = 0,
        [string]$ExpectedVehicleMake = "",
        [string]$ExpectedVehicleModel = ""
    )

    if (
        $null -eq $InitialCurrent -or
        -not $InitialCurrent.vehicle_hint_applied -or
        $null -eq $InitialCurrent.row_element -or
        $InitialCurrent.resolution_status -notin @("view_not_found", "inspection_id_missing")
    ) {
        return @{
            success = $false
            diagnostics = @{
                status = "row_expand_not_authorized"
                initial_status = if ($InitialCurrent) { $InitialCurrent.resolution_status } else { $null }
            }
        }
    }

    $attempts = @()
    $lastCurrent = $InitialCurrent
    $lastDocument = $null
    foreach ($method in @("invoke_pattern", "dpi_aware_mouse")) {
        # Never reuse a potentially stale row/edit element after a Vue render.
        $document = Find-Web-Document -ChromeRoot $ChromeRoot
        if ($null -eq $document) {
            $attempts += @{ method = $method; attempted = $false; status = "web_document_not_found" }
            continue
        }
        $fresh = Read-Current-Ro `
            -Root $document `
            -Ro $Ro `
            -ExpectedInspectionId $ExpectedInspectionId `
            -ExpectedVehicleYear $ExpectedVehicleYear `
            -ExpectedVehicleMake $ExpectedVehicleMake `
            -ExpectedVehicleModel $ExpectedVehicleModel
        if (-not (Test-Same-Observed-Vehicle-Row -ExpectedCurrent $InitialCurrent -ActualCurrent $fresh)) {
            return @{
                success = $false
                diagnostics = @{
                    status = "row_expand_target_changed"
                    attempts = $attempts
                    post_status = $fresh.resolution_status
                    post_vin = $fresh.vin
                    post_vehicle = $fresh.vehicle
                }
            }
        }
        if ($fresh.resolution_status -eq "resolved" -and $fresh.row_binding_confirmed) {
            return @{
                success = $true
                current = $fresh
                document = $document
                diagnostics = @{
                    status = "row_already_expanded"
                    attempts = $attempts
                    vin = $fresh.vin
                    inspection_id = $fresh.inspection_id
                }
            }
        }
        if ($fresh.resolution_status -notin @("view_not_found", "inspection_id_missing")) {
            return @{
                success = $false
                diagnostics = @{
                    status = "row_expand_state_unproven"
                    attempts = $attempts
                    post_status = $fresh.resolution_status
                }
            }
        }

        $edit = Resolve-Exact-Row-EditLeaf -Row $fresh.row_element
        if ($edit.status -ne "row_edit_resolved") {
            return @{
                success = $false
                diagnostics = @{
                    status = $edit.status
                    attempts = $attempts
                    edit_candidates = $edit.candidates
                }
            }
        }
        $attempted = if ($method -eq "invoke_pattern") {
            Invoke-InvokePatternOnly -Element $edit.element
        } else {
            Click-ElementCenter -Element $edit.element
        }
        $attempt = @{
            method = $method
            attempted = [bool]$attempted
            edit_control = $edit.candidates[0]
            samples = @()
        }
        $attempts += $attempt
        if (-not $attempted) { continue }

        for ($poll = 0; $poll -lt 10; $poll++) {
            Start-Sleep -Milliseconds 300
            $document = Find-Web-Document -ChromeRoot $ChromeRoot
            if ($null -eq $document) {
                $attempt.samples += @{ poll = ($poll + 1); status = "web_document_not_found" }
                continue
            }
            $refreshed = Read-Current-Ro `
                -Root $document `
                -Ro $Ro `
                -ExpectedInspectionId $ExpectedInspectionId `
                -ExpectedVehicleYear $ExpectedVehicleYear `
                -ExpectedVehicleMake $ExpectedVehicleMake `
                -ExpectedVehicleModel $ExpectedVehicleModel
            $sameTarget = Test-Same-Observed-Vehicle-Row `
                -ExpectedCurrent $InitialCurrent `
                -ActualCurrent $refreshed
            $attempt.samples += @{
                poll = ($poll + 1)
                same_target = [bool]$sameTarget
                status = $refreshed.resolution_status
                vin = $refreshed.vin
                inspection_id = $refreshed.inspection_id
            }
            $lastCurrent = $refreshed
            $lastDocument = $document
            if (
                $sameTarget -and
                $refreshed.resolution_status -eq "resolved" -and
                $refreshed.row_binding_confirmed -and
                $null -ne $refreshed.view_element -and
                -not [string]::IsNullOrWhiteSpace([string]$refreshed.inspection_id)
            ) {
                return @{
                    success = $true
                    current = $refreshed
                    document = $document
                    diagnostics = @{
                        status = "row_expanded"
                        activation_method = $method
                        attempts = $attempts
                        vin = $refreshed.vin
                        vehicle = $refreshed.vehicle
                        inspection_id = $refreshed.inspection_id
                        row_binding_confirmed = $refreshed.row_binding_confirmed
                    }
                }
            }
        }

        # An inspection-like subtree appeared but did not prove its exact View
        # binding. Do not click the edit toggle again and risk collapsing it.
        if ($lastCurrent -and $lastCurrent.resolution_status -ne "view_not_found") { break }
    }
    return @{
        success = $false
        current = $lastCurrent
        document = $lastDocument
        diagnostics = @{
            status = "post_expand_view_unproven"
            attempts = $attempts
            post_status = if ($lastCurrent) { $lastCurrent.resolution_status } else { $null }
            post_vin = if ($lastCurrent) { $lastCurrent.vin } else { $null }
            post_vehicle = if ($lastCurrent) { $lastCurrent.vehicle } else { $null }
        }
    }
}


function Wait-For-Stable-Ro-Grid {
    param(
        [System.Windows.Automation.AutomationElement]$ChromeRoot,
        [string]$Ro,
        [string]$ExpectedInspectionId = "",
        [int]$ExpectedVehicleYear = 0,
        [string]$ExpectedVehicleMake = "",
        [string]$ExpectedVehicleModel = "",
        [int]$Polls = 12
    )

    $last = $null
    $lastDocument = $null
    $lastFingerprint = $null
    $stableResolvedSamples = 0
    $samples = @()
    for ($poll = 0; $poll -lt $Polls; $poll++) {
        Start-Sleep -Milliseconds 500
        $document = Find-Web-Document -ChromeRoot $ChromeRoot
        if ($null -eq $document) {
            $samples += @{ poll = ($poll + 1); status = "web_document_not_found" }
            continue
        }
        $lastDocument = $document
        $current = Read-Current-Ro `
            -Root $document `
            -Ro $Ro `
            -ExpectedInspectionId $ExpectedInspectionId `
            -ExpectedVehicleYear $ExpectedVehicleYear `
            -ExpectedVehicleMake $ExpectedVehicleMake `
            -ExpectedVehicleModel $ExpectedVehicleModel
        $last = $current
        $fingerprint = @(
            [string]$current.resolution_status,
            [string]$current.hit_count,
            [string]$current.vin,
            [string]$current.inspection_id,
            [string]$current.vehicle.year,
            [string]$current.vehicle.make,
            [string]$current.vehicle.model_configuration
        ) -join "|"
        if ($current.found -and $current.resolution_status -eq "resolved") {
            if ($fingerprint -eq $lastFingerprint) {
                $stableResolvedSamples++
            }
            else {
                $stableResolvedSamples = 1
            }
        }
        else {
            $stableResolvedSamples = 0
        }
        $lastFingerprint = $fingerprint
        $samples += @{
            poll = ($poll + 1)
            found = [bool]$current.found
            status = $current.resolution_status
            hit_count = $current.hit_count
            stable_resolved_samples = $stableResolvedSamples
        }
        # Three identical half-second samples prevent a partially-rendered
        # first duplicate row from winning before the rest of the grid arrives.
        if ($stableResolvedSamples -ge 3) { break }
    }
    return @{
        current = $last
        document = $lastDocument
        samples = $samples
        stable_resolved = ($stableResolvedSamples -ge 3)
    }
}

# ADAS Map filters the whole file grid by a single selected "business"
# (shop location). Different shops' RO's are invisible to search until the
# matching business is selected -- searching without switching first looks
# exactly like "RO not found" even though the RO genuinely exists.
$ShopsByRoPrefix = @{
    "24009" = @{ name = "Gerber Collision & Glass - Macon/Mercer University"; query = "Macon" }
    "24006" = @{ name = "Gerber Collision & Glass - Perry (GA)"; query = "Perry" }
    "24007" = @{ name = "Gerber Collision & Glass - Warner Robins"; query = "Warner Robins" }
}

function Get-Shop-For-Ro {
    param([string]$Ro)

    $clean = [string]$Ro
    if ($clean.Length -lt 5) { return $null }
    $prefix = $clean.Substring(0, 5)
    if ($ShopsByRoPrefix.ContainsKey($prefix)) {
        return $ShopsByRoPrefix[$prefix]
    }
    return $null
}

function Get-Selected-Business-Text {
    param([System.Windows.Automation.AutomationElement]$Root)

    $searchEdit = Find-Search-Edit -Root $Root
    if ($null -eq $searchEdit) { return $null }
    try { $searchRect = $searchEdit.element.Current.BoundingRectangle } catch { return $null }

    $desc = Descendants -Root $Root
    $selectedLabels = @(
        $desc |
        Where-Object {
            try {
                if (
                    $_.Current.IsOffscreen -or
                    $_.Current.ControlType -ne [System.Windows.Automation.ControlType]::Text -or
                    [string]$_.Current.Name -notmatch "(?i)^\s*Gerber Collision"
                ) { return $false }
                $rect = $_.Current.BoundingRectangle
                $centerY = $rect.Top + ($rect.Height / 2)
                return (
                    $rect.Width -gt 0 -and
                    $rect.Height -gt 0 -and
                    $centerY -ge ($searchRect.Top - 8) -and
                    $centerY -le ($searchRect.Top + $searchRect.Height + 8)
                )
            }
            catch { return $false }
        }
    )
    $selectedNames = @(
        $selectedLabels |
        ForEach-Object { ([string]$_.Current.Name).Trim() } |
        Where-Object { $_ } |
        Select-Object -Unique
    )
    if ($selectedNames.Count -eq 1) { return $selectedNames[0] }
    return $null
}

function Select-Business {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$TargetName,
        [string]$SearchQuery
    )

    $current = Get-Selected-Business-Text -Root $Root
    if ($current -and $current -eq $TargetName) {
        return @{ changed = $false; selected = $current }
    }

    # A prior RO's inspection detail (the Cancel/Complete Inspection modal)
    # can still be open over the list screen at this point -- the toolbar's
    # business label is often still readable behind it (so the check above
    # can pass), but clicking into the selector then lands on the modal
    # instead, which looks identical to "no matching option" downstream.
    # Confirmed live: this is what actually broke every shop switch after
    # the first RO of a run. Close it before touching the selector.
    $modalOpen = $null -ne ((Descendants -Root $Root) | Where-Object {
        -not $_.Current.IsOffscreen -and [string]$_.Current.Name -eq "Complete Inspection"
    } | Select-Object -First 1)
    if ($modalOpen) {
        # Different inspection states expose different dismiss buttons --
        # observed live: some show "Cancel", some show "Close", and at least
        # one ("No-Scrub Inspections") shows neither, which silently left
        # the modal open and broke every business switch after it. Try
        # whichever named button exists, and send Escape unconditionally
        # too as a button-name-independent fallback (safe no-op if nothing
        # is actually open to dismiss).
        $dismissBtn = (Descendants -Root $Root) | Where-Object {
            -not $_.Current.IsOffscreen -and
            ([string]$_.Current.Name -eq "Cancel" -or [string]$_.Current.Name -eq "Close")
        } | Select-Object -First 1
        if ($null -ne $dismissBtn) {
            Click-ElementCenter -Element $dismissBtn | Out-Null
            Start-Sleep -Milliseconds 600
        }
        try { [System.Windows.Forms.SendKeys]::SendWait("{ESC}") } catch {}
        Start-Sleep -Milliseconds 400
    }

    # The multiselect's own input has a degenerate (offscreen/zero-size)
    # rectangle whether or not a business is currently selected, so
    # SetFocus() on it reliably throws -- the real click target is the
    # visible label next to it: the currently-selected business name when
    # one is selected, or the "type to select a business" placeholder when
    # nothing is selected yet. The element can be momentarily missing from
    # a fresh Descendants scan right after page/business-grid transitions,
    # so retry briefly rather than failing on a single timing miss.
    # NOTE: the multiselect's hidden input (class multiselect__input, a
    # degenerate/infinite BoundingRectangle) shares the exact same
    # accessible Name as the real visible label. Matching by name alone can
    # pick whichever happens to come first in document order -- filtering
    # to ControlType.Text (as Get-Selected-Business-Text already does)
    # is what actually excludes the unclickable input.
    # Observed live after a page reload with nothing selected: the same
    # placeholder text can instead surface as ControlType.Edit rather than
    # ControlType.Text. The degenerate hidden input this filter exists to
    # exclude always has an infinite/zero-size BoundingRectangle regardless
    # of its ControlType, so checking the rectangle directly is what
    # actually distinguishes them -- not the control type alone.
    $label = $null
    for ($i = 0; $i -lt 6 -and $null -eq $label; $i++) {
        if ($i -gt 0) { Start-Sleep -Milliseconds 300 }
        $desc = Descendants -Root $Root
        $label = $desc | Where-Object {
            -not $_.Current.IsOffscreen -and
            (
                $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Text -or
                $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Edit
            ) -and
            (
                [string]$_.Current.Name -match "(?i)^\s*Gerber Collision" -or
                [string]$_.Current.Name -match "(?i)^\s*type to select a business\s*$"
            ) -and
            $_.Current.BoundingRectangle.Width -gt 0 -and
            $_.Current.BoundingRectangle.Height -gt 0 -and
            -not [double]::IsInfinity($_.Current.BoundingRectangle.Left)
        } | Select-Object -First 1
    }

    $opened = $false
    if ($null -ne $label) {
        $opened = Click-ElementCenter -Element $label
        # A synthetic click alone was observed live to not reliably transfer
        # keyboard focus to this element when it renders as a real
        # ControlType.Edit (the "nothing selected yet" state) rather than
        # the Text-label proxy -- typed keys then went nowhere and the
        # dropdown showed no results. Explicit SetFocus() fixed it; degrades
        # safely since the historically-degenerate hidden input already
        # throws here (documented above), which this simply swallows.
        try { $label.SetFocus() } catch {}
    }
    if (-not $opened) {
        return @{ changed = $false; error = "business_selector_not_found" }
    }

    Start-Sleep -Milliseconds 400
    [System.Windows.Forms.SendKeys]::SendWait("^a")
    Start-Sleep -Milliseconds 100
    [System.Windows.Forms.SendKeys]::SendWait("{DEL}")
    Start-Sleep -Milliseconds 150
    [System.Windows.Forms.SendKeys]::SendWait($SearchQuery)
    # 700ms was not reliably enough for the filtered list to populate --
    # confirmed live: a manual reproduction with a longer wait found the
    # exact matching item every time. Poll instead of a single fixed wait.
    $match = $null
    for ($wait = 0; $wait -lt 6 -and $null -eq $match; $wait++) {
        Start-Sleep -Milliseconds 300
        $desc2 = Descendants -Root $Root
        $match = $desc2 | Where-Object {
            -not $_.Current.IsOffscreen -and
            $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::ListItem -and
            ([string]$_.Current.Name).StartsWith($TargetName)
        } | Select-Object -First 1
    }

    if ($null -eq $match) {
        $diagEdit = $desc2 | Where-Object {
            -not $_.Current.IsOffscreen -and
            $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Edit -and
            [string]$_.Current.Name -match "(?i)business"
        } | Select-Object -First 1
        $diagValue = $null
        if ($null -ne $diagEdit) {
            try { $diagValue = $diagEdit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value } catch {}
        }
        $diagItems = @($desc2 | Where-Object {
            -not $_.Current.IsOffscreen -and $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::ListItem
        } | ForEach-Object { [string]$_.Current.Name })
        return @{
            changed = $false
            error = "business_option_not_found"
            query = $SearchQuery
            diag_edit_value = $diagValue
            diag_list_items = $diagItems
        }
    }

    # Vue list options have been observed reporting a successful legacy
    # default action without changing the selected business. Use a physical
    # click first, then prove the toolbar's selected label changed; a visible
    # dropdown option is deliberately not accepted as selection evidence.
    $clicked = Click-ElementCenter -Element $match
    $after = $null
    for ($poll = 0; $poll -lt 8; $poll++) {
        Start-Sleep -Milliseconds 250
        try { $after = Get-Selected-Business-Text -Root $Root } catch { $after = $null }
        if ($after -eq $TargetName) { break }
    }
    if ($after -ne $TargetName) {
        $legacyAttempted = Invoke-LegacyDefaultAction -Element $match
        $clicked = ($clicked -or $legacyAttempted)
        for ($poll = 0; $poll -lt 8; $poll++) {
            Start-Sleep -Milliseconds 250
            try { $after = Get-Selected-Business-Text -Root $Root } catch { $after = $null }
            if ($after -eq $TargetName) { break }
        }
    }

    return @{
        changed = ($after -eq $TargetName)
        selected = $after
        clicked = $clicked
        selection_confirmed = ($after -eq $TargetName)
    }
}

try {
    $windows = @(Get-Chrome-Windows)
    $target = Select-AdasMap-Window -Windows $windows

    $windowSummary = @(
        $windows |
        ForEach-Object {
            [pscustomobject]@{
                title = $_.title
                process_id = $_.process_id
                handle = $_.handle
                candidate = (
                    $_.title -match
                    "(?i)\bADAS\b|ADAS\s*Map|gerber\.adasmap|opus\.adasmap"
                )
            }
        }
    )

    if ($Action -eq "status") {
        [pscustomobject]@{
            success = $true
            action = "status"
            target_found = ($null -ne $target)
            target = if ($target) {
                @{
                    title = $target.title
                    process_id = $target.process_id
                    handle = $target.handle
                }
            } else { $null }
            chrome_windows = $windowSummary
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    if ($Action -eq "open") {
        # Interactive sign-in handoff. When an ADAS Map window already exists
        # (even a login page) it is only fronted so the operator can sign in
        # there; a brand-new Chrome window/tab is launched only when no ADAS
        # Map window is visible at all, so repeated open calls never stack
        # duplicate sign-in tabs. Credentials are never read or entered here.
        if ($null -ne $target) {
            $fronted = Bring-To-Front -Target $target
            [pscustomobject]@{
                success = $true
                action = "open"
                status = "focused_existing"
                launched = $false
                focused = [bool]$fronted
                target_found = $true
                target = @{
                    title = $target.title
                    process_id = $target.process_id
                    handle = $target.handle
                }
                chrome_windows = $windowSummary
            } | ConvertTo-Json -Depth 8 -Compress
            exit 0
        }

        if ([string]::IsNullOrWhiteSpace($HomeUrl)) {
            [pscustomobject]@{
                success = $false
                action = "open"
                status = "home_url_missing"
                launched = $false
                focused = $false
                target_found = $false
                message = "No ADAS Map window is open and no -HomeUrl was provided to launch one."
                chrome_windows = $windowSummary
            } | ConvertTo-Json -Depth 8 -Compress
            exit 0
        }

        $chromeExe = Resolve-Chrome-Executable
        if ($null -eq $chromeExe) {
            [pscustomobject]@{
                success = $false
                action = "open"
                status = "chrome_not_found"
                launched = $false
                focused = $false
                target_found = $false
                message = "Google Chrome could not be located to open the managed ADAS Map sign-in page."
                chrome_windows = $windowSummary
            } | ConvertTo-Json -Depth 8 -Compress
            exit 0
        }

        $launchArgs = @()
        if (-not [string]::IsNullOrWhiteSpace($ChromeProfile)) {
            $launchArgs += "--profile-directory=$ChromeProfile"
        }
        $launchArgs += $HomeUrl
        Start-Process -FilePath $chromeExe -ArgumentList $launchArgs | Out-Null

        # Poll briefly for the ADAS Map-titled window so the caller learns
        # whether the sign-in page actually became visible.
        $opened = $null
        for ($poll = 0; $poll -lt 24; $poll++) {
            Start-Sleep -Milliseconds 500
            $windowsNow = @(Get-Chrome-Windows)
            $opened = Select-AdasMap-Window -Windows $windowsNow
            if ($null -ne $opened) { break }
        }
        $frontedNew = $false
        if ($null -ne $opened) {
            $frontedNew = [bool](Bring-To-Front -Target $opened)
        }
        [pscustomobject]@{
            success = ($null -ne $opened)
            action = "open"
            status = if ($null -ne $opened) { "launched" } else { "launch_unverified" }
            launched = $true
            focused = $frontedNew
            target_found = ($null -ne $opened)
            target = if ($null -ne $opened) {
                @{
                    title = $opened.title
                    process_id = $opened.process_id
                    handle = $opened.handle
                }
            } else { $null }
            message = if ($null -ne $opened) {
                "The managed ADAS Map sign-in window was opened; interactive sign-in may be required."
            } else {
                "Chrome was launched at the ADAS Map home page, but no ADAS Map-titled window became visible yet."
            }
            chrome_windows = $windowSummary
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    if ($Action -eq "read-window") {
        # Exploration only: dump whatever a Chrome window currently shows,
        # matched by a title substring rather than the ADAS-specific title
        # regex Select-AdasMap-Window uses -- needed because a click can
        # navigate the ADAS Map window itself to a different site (e.g.
        # ALLDATA), at which point it no longer matches an "ADAS" title but
        # is still the same window worth reading.
        if ([string]::IsNullOrWhiteSpace($WindowTitleContains)) {
            throw "read-window requires -WindowTitleContains"
        }
        $matches = @(
            $windows | Where-Object { $_.title -match [regex]::Escape($WindowTitleContains) }
        )
        if ($matches.Count -eq 0) {
            [pscustomobject]@{
                success = $false
                action = "read-window"
                status = "window_not_found"
                window_title_contains = $WindowTitleContains
                chrome_windows = $windowSummary
            } | ConvertTo-Json -Depth 8 -Compress
            exit 0
        }
        $windowTarget = $matches[0]
        $snapshot = Document-Snapshot -Document $windowTarget.element -Limit 2000
        [pscustomobject]@{
            success = $true
            action = "read-window"
            status = "window_read"
            window_title = $windowTarget.title
            window_handle = $windowTarget.handle
            value_count = $snapshot.count
            values = @($snapshot.values)
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    if ($Action -eq "return-to-adas") {
        $returned = Return-To-Adas-Tab
        $windowsAfter = @(Get-Chrome-Windows)
        $adasAfter = Select-AdasMap-Window -Windows $windowsAfter
        [pscustomobject]@{
            success = $returned
            action = "return-to-adas"
            status = if ($returned) { "on_adas_map" } else { "return_failed" }
            target_title = if ($adasAfter) { $adasAfter.title } else { $null }
        } | ConvertTo-Json -Depth 6 -Compress
        exit 0
    }

    if ($null -eq $target) {
        [pscustomobject]@{
            success = $false
            action = $Action
            status = "adas_map_window_not_found"
            message = "No open Chrome window with an ADAS Map title was found."
            chrome_windows = $windowSummary
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    $foreground = Bring-To-Front -Target $target

    if ($Action -eq "close-details") {
        $closed = Close-Inspection-Modal -Root $target.element
        [pscustomobject]@{
            success = [bool]$closed.closed
            action = "close-details"
            status = if ($closed.closed) { "details_closed" } else { "details_not_open" }
            result = $closed
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    if ($Action -eq "inspect") {
        $document = Find-Web-Document -ChromeRoot $target.element
        if ($null -eq $document) {
            [pscustomobject]@{
                success = $false
                action = "inspect"
                status = "web_document_not_found"
                message = "Chrome did not expose the ADAS Map web Document; browser chrome was not inspected."
            } | ConvertTo-Json -Depth 8 -Compress
            exit 0
        }
        $controls = @(Visible-Control-Data -Root $document -Limit 400)
        $searchEdit = Find-Search-Edit -Root $document

        [pscustomobject]@{
            success = $true
            action = "inspect"
            target = @{
                title = $target.title
                process_id = $target.process_id
                handle = $target.handle
            }
            foreground_requested = $foreground
            search_control_found = ($null -ne $searchEdit)
            search_control = if ($searchEdit) { $searchEdit.data } else { $null }
            controls = $controls
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    if ($Action -eq "read-current") {
        if ([string]::IsNullOrWhiteSpace($RoNumber)) {
            throw "read-current requires -RoNumber"
        }

        $document = Find-Web-Document -ChromeRoot $target.element
        if ($null -eq $document) {
            [pscustomobject]@{
                success = $false
                action = "read-current"
                status = "web_document_not_found"
                ro_number = $RoNumber
            } | ConvertTo-Json -Depth 8 -Compress
            exit 0
        }
        $current = Read-Current-Ro `
            -Root $document `
            -Ro $RoNumber `
            -ExpectedInspectionId $InspectionId `
            -ExpectedVehicleYear $ExpectedYear `
            -ExpectedVehicleMake $ExpectedMake `
            -ExpectedVehicleModel $ExpectedModel

        [pscustomobject]@{
            success = ($current.found -and $current.resolution_status -eq "resolved" -and $null -ne $current.vehicle)
            action = "read-current"
            status = if (-not $current.found) {
                "ro_not_visible"
            }
            elseif ($current.resolution_status -ne "resolved") {
                $current.resolution_status
            }
            elseif ($null -eq $current.vehicle) {
                "vehicle_identity_unparsed"
            }
            else {
                "ro_visible"
            }
            ro_number = $RoNumber
            target_title = $target.title
            foreground_requested = $foreground
            vin = $current.vin
            vehicle = $current.vehicle
            values = $current.values
            view_available = $current.view_available
            inspection_id = $current.inspection_id
            row_binding_confirmed = $current.row_binding_confirmed
            row_scope = $current.row_scope
            association_scope = $current.association_scope
            inspection_scope = $current.inspection_scope
            view_candidates = $current.view_candidates
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    if ($Action -eq "lookup") {
        if ([string]::IsNullOrWhiteSpace($RoNumber)) {
            throw "lookup requires -RoNumber"
        }

        $document = Find-Web-Document -ChromeRoot $target.element
        if ($null -eq $document) {
            [pscustomobject]@{
                success = $false
                action = "lookup"
                status = "web_document_not_found"
                ro_number = $RoNumber
            } | ConvertTo-Json -Depth 8 -Compress
            exit 0
        }
        $scope = $document

        # The file grid is scoped to one selected business/shop at a time.
        # An RO from a different shop is invisible to search until that
        # shop is selected -- without this it looks identical to "not found".
        $shop = Get-Shop-For-Ro -Ro $RoNumber
        $shopSwitch = $null
        if ($null -ne $shop) {
            $shopSwitch = Select-Business -Root $scope -TargetName $shop.name -SearchQuery $shop.query

            if ($shopSwitch.error) {
                [pscustomobject]@{
                    success = $false
                    action = "lookup"
                    status = "business_selection_failed"
                    ro_number = $RoNumber
                    shop = $shop.name
                    shop_switch = $shopSwitch
                } | ConvertTo-Json -Depth 8 -Compress
                exit 0
            }

            if ($shopSwitch.selected -ne $shop.name) {
                # Fail closed: never search under an unconfirmed/wrong
                # business -- that looks identical to "RO not found" and
                # would be silently wrong instead of loudly wrong.
                [pscustomobject]@{
                    success = $false
                    action = "lookup"
                    status = "business_selection_unconfirmed"
                    ro_number = $RoNumber
                    shop = $shop.name
                    shop_switch = $shopSwitch
                } | ConvertTo-Json -Depth 8 -Compress
                exit 0
            }

            if ($shopSwitch.changed) {
                # Switching business reloads the grid; re-resolve the document.
                Start-Sleep -Milliseconds 500
                $document = Find-Web-Document -ChromeRoot $target.element
                if ($null -eq $document) {
                    [pscustomobject]@{
                        success = $false
                        action = "lookup"
                        status = "web_document_not_found"
                        ro_number = $RoNumber
                    } | ConvertTo-Json -Depth 8 -Compress
                    exit 0
                }
                $scope = $document
            }
        }
        $observedShop = Get-Selected-Business-Text -Root $scope
        if ($null -ne $shop -and $observedShop -ne $shop.name) {
            [pscustomobject]@{
                success = $false
                action = "lookup"
                status = "business_selection_unconfirmed"
                ro_number = $RoNumber
                shop = $shop.name
                observed_shop = $observedShop
                shop_switch = $shopSwitch
            } | ConvertTo-Json -Depth 8 -Compress
            exit 0
        }

        # Critical optimization: if the operator already pulled the RO up, do
        # not disturb a proven expanded row. A collapsed duplicate-RO target
        # may activate only its own uniquely-bound edit leaf below.
        $current = Read-Current-Ro `
            -Root $scope `
            -Ro $RoNumber `
            -ExpectedInspectionId $InspectionId `
            -ExpectedVehicleYear $ExpectedYear `
            -ExpectedVehicleMake $ExpectedMake `
            -ExpectedVehicleModel $ExpectedModel
        $rowExpansion = $null
        if (
            $current.found -and
            $current.vin -and
            $current.vehicle_hint_applied -and
            $current.resolution_status -in @("view_not_found", "inspection_id_missing")
        ) {
            $expanded = Expand-Proven-Vehicle-Row `
                -ChromeRoot $target.element `
                -Ro $RoNumber `
                -InitialCurrent $current `
                -ExpectedInspectionId $InspectionId `
                -ExpectedVehicleYear $ExpectedYear `
                -ExpectedVehicleMake $ExpectedMake `
                -ExpectedVehicleModel $ExpectedModel
            $rowExpansion = $expanded.diagnostics
            if ($expanded.success) {
                $current = $expanded.current
                if ($null -ne $expanded.document) { $scope = $expanded.document }
            }
        }

        if ($current.found -and $current.vin) {
            if ($current.resolution_status -ne "resolved") {
                [pscustomobject]@{
                    success = $false
                    action = "lookup"
                    status = $current.resolution_status
                    ro_number = $RoNumber
                    vin = $current.vin
                    vehicle = $current.vehicle
                    observed_shop = $observedShop
                    values = $current.values
                    vehicle_hint_applied = $current.vehicle_hint_applied
                    candidate_vehicles = $current.candidate_vehicles
                    view_candidates = $current.view_candidates
                    row_expansion = $rowExpansion
                } | ConvertTo-Json -Depth 9 -Compress
                exit 0
            }
            if ($null -eq $current.vehicle) {
                [pscustomobject]@{
                    success = $false
                    action = "lookup"
                    status = "vehicle_identity_unparsed"
                    ro_number = $RoNumber
                    vin = $current.vin
                    vehicle = $null
                    observed_shop = $observedShop
                    values = $current.values
                    inspection_id = $current.inspection_id
                    view_candidates = $current.view_candidates
                } | ConvertTo-Json -Depth 9 -Compress
                exit 0
            }
            [pscustomobject]@{
                success = $true
                action = "lookup"
                status = "ro_visible"
                ro_number = $RoNumber
                target_title = $target.title
                foreground_requested = $foreground
                search_skipped = $true
                shop_switch = $shopSwitch
                observed_shop = $observedShop
                vin = $current.vin
                vehicle = $current.vehicle
                values = $current.values
                view_available = $current.view_available
                inspection_id = $current.inspection_id
                row_binding_confirmed = $current.row_binding_confirmed
                vehicle_hint_applied = $current.vehicle_hint_applied
                row_expansion = $rowExpansion
                row_scope = $current.row_scope
                association_scope = $current.association_scope
                inspection_scope = $current.inspection_scope
                view_candidates = $current.view_candidates
            } | ConvertTo-Json -Depth 8 -Compress
            exit 0
        }

        $searchAttempts = @()
        $searchAction = $null
        $searchEdit = $null
        $after = $null
        $maxSearchAttempts = if ($shopSwitch -and $shopSwitch.changed) { 2 } else { 1 }
        for ($searchAttempt = 1; $searchAttempt -le $maxSearchAttempts; $searchAttempt++) {
            # Re-resolve the Document on every retry because switching business
            # asynchronously rebuilds the Vue grid and can stale prior elements.
            $scope = Find-Web-Document -ChromeRoot $target.element
            if ($null -eq $scope) {
                $searchAttempts += @{ attempt = $searchAttempt; status = "web_document_not_found" }
                continue
            }
            $observedShop = Get-Selected-Business-Text -Root $scope
            if ($null -ne $shop -and $observedShop -ne $shop.name) {
                [pscustomobject]@{
                    success = $false
                    action = "lookup"
                    status = "business_selection_unconfirmed"
                    ro_number = $RoNumber
                    shop = $shop.name
                    observed_shop = $observedShop
                    shop_switch = $shopSwitch
                    search_attempts = $searchAttempts
                } | ConvertTo-Json -Depth 9 -Compress
                exit 0
            }

            $searchEdit = Find-Search-Edit -Root $scope
            if ($null -eq $searchEdit) {
                $searchAttempts += @{ attempt = $searchAttempt; status = "search_control_not_found" }
                continue
            }
            $valueSet = Set-Element-Value `
                -Element $searchEdit.element `
                -Value $RoNumber
            if (-not $valueSet) {
                $searchAttempts += @{ attempt = $searchAttempt; status = "search_value_failed" }
                continue
            }

            $searchAction = Invoke-Search -Root $scope
            $wait = Wait-For-Stable-Ro-Grid `
                -ChromeRoot $target.element `
                -Ro $RoNumber `
                -ExpectedInspectionId $InspectionId `
                -ExpectedVehicleYear $ExpectedYear `
                -ExpectedVehicleMake $ExpectedMake `
                -ExpectedVehicleModel $ExpectedModel
            if ($null -ne $wait.document) { $scope = $wait.document }
            $after = $wait.current
            $searchAttempts += @{
                attempt = $searchAttempt
                value_set = $true
                search_action = $searchAction
                stable_resolved = $wait.stable_resolved
                result_status = if ($after) { $after.resolution_status } else { "ro_not_visible" }
                samples = $wait.samples
            }
            if ($after -and $after.found) { break }
        }

        if ($null -eq $after) {
            $after = @{
                found = $false
                resolution_status = "ro_not_visible"
                vin = $null
                vehicle = $null
                values = @()
                view_available = $false
                inspection_id = $null
                row_binding_confirmed = $false
                view_candidates = @()
            }
        }
        $observedShop = if ($null -ne $scope) { Get-Selected-Business-Text -Root $scope } else { $null }

        $rowExpansion = $null
        if (
            $after.found -and
            $after.vin -and
            $after.vehicle_hint_applied -and
            $after.resolution_status -in @("view_not_found", "inspection_id_missing")
        ) {
            $expanded = Expand-Proven-Vehicle-Row `
                -ChromeRoot $target.element `
                -Ro $RoNumber `
                -InitialCurrent $after `
                -ExpectedInspectionId $InspectionId `
                -ExpectedVehicleYear $ExpectedYear `
                -ExpectedVehicleMake $ExpectedMake `
                -ExpectedVehicleModel $ExpectedModel
            $rowExpansion = $expanded.diagnostics
            if ($expanded.success) {
                $after = $expanded.current
                if ($null -ne $expanded.document) { $scope = $expanded.document }
            }
        }

        $lastSearchFailure = @(
            $searchAttempts |
            Where-Object {
                $_.status -in @(
                    "web_document_not_found",
                    "search_control_not_found",
                    "search_value_failed"
                )
            }
        ) | Select-Object -Last 1
        $afterStatus = if (-not $after.found -and $null -eq $searchAction -and $lastSearchFailure) {
            $lastSearchFailure.status
        }
        elseif (-not $after.found) {
            "no_ro_match_visible"
        }
        elseif ($after.resolution_status -eq "resolved") {
            if ($null -eq $after.vehicle) { "vehicle_identity_unparsed" } else { "ro_visible" }
        }
        else {
            $after.resolution_status
        }

        [pscustomobject]@{
            success = ($afterStatus -eq "ro_visible")
            action = "lookup"
            status = $afterStatus
            ro_number = $RoNumber
            target_title = $target.title
            foreground_requested = $foreground
            search_skipped = $false
            shop_switch = $shopSwitch
            observed_shop = $observedShop
            search_control = $searchEdit.data
            search_action = $searchAction
            search_attempts = $searchAttempts
            vin = $after.vin
            vehicle = $after.vehicle
            values = $after.values
            view_available = $after.view_available
            inspection_id = $after.inspection_id
            row_binding_confirmed = $after.row_binding_confirmed
            vehicle_hint_applied = $after.vehicle_hint_applied
            candidate_vehicles = $after.candidate_vehicles
            row_expansion = $rowExpansion
            row_scope = $after.row_scope
            association_scope = $after.association_scope
            inspection_scope = $after.inspection_scope
            view_candidates = $after.view_candidates
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }


if ($Action -eq "details") {
    if ([string]::IsNullOrWhiteSpace($RoNumber)) {
        throw "details requires -RoNumber"
    }

    $document = Find-Web-Document -ChromeRoot $target.element
    if ($null -eq $document) {
        [pscustomobject]@{
            success = $false
            action = "details"
            status = "web_document_not_found"
            ro_number = $RoNumber
            message = "Chrome did not expose the ADAS Map web document."
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    $before = Document-Snapshot -Document $document
    $beforeSignature = Snapshot-Signature -Snapshot $before

    $current = Read-Current-Ro `
        -Root $document `
        -Ro $RoNumber `
        -ExpectedInspectionId $InspectionId `
        -ExpectedVehicleYear $ExpectedYear `
        -ExpectedVehicleMake $ExpectedMake `
        -ExpectedVehicleModel $ExpectedModel
    $rowExpansion = $null
    if (
        $current.found -and
        $current.vin -and
        $current.vehicle_hint_applied -and
        $current.resolution_status -in @("view_not_found", "inspection_id_missing")
    ) {
        $expanded = Expand-Proven-Vehicle-Row `
            -ChromeRoot $target.element `
            -Ro $RoNumber `
            -InitialCurrent $current `
            -ExpectedInspectionId $InspectionId `
            -ExpectedVehicleYear $ExpectedYear `
            -ExpectedVehicleMake $ExpectedMake `
            -ExpectedVehicleModel $ExpectedModel
        $rowExpansion = $expanded.diagnostics
        if ($expanded.success) {
            $current = $expanded.current
            if ($null -ne $expanded.document) { $document = $expanded.document }
        }
    }

    if (-not $current.found) {
        [pscustomobject]@{
            success = $false
            action = "details"
            status = "ro_not_visible"
            ro_number = $RoNumber
            page_values = @($before.values | Select-Object -First 180)
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    if ($current.resolution_status -ne "resolved") {
        [pscustomobject]@{
            success = $false
            action = "details"
            status = $current.resolution_status
            ro_number = $RoNumber
            vin = $current.vin
            vehicle = $current.vehicle
            row_values = $current.values
            inspection_id = $current.inspection_id
            view_candidates_seen = $current.view_candidates
            row_expansion = $rowExpansion
            page_values = @($before.values | Select-Object -First 180)
        } | ConvertTo-Json -Depth 9 -Compress
        exit 0
    }

    if ($null -eq $current.vehicle) {
        [pscustomobject]@{
            success = $false
            action = "details"
            status = "vehicle_identity_unparsed"
            ro_number = $RoNumber
            vin = $current.vin
            vehicle = $null
            row_values = $current.values
            inspection_id = $current.inspection_id
            view_candidates_seen = $current.view_candidates
            row_expansion = $rowExpansion
        } | ConvertTo-Json -Depth 9 -Compress
        exit 0
    }

    $view = $current.view_element
    $resolvedInspectionId = [string]$current.inspection_id
    $viewData = Element-Data -Element $view
    $beforeDetailState = Detail-State `
        -Document $document `
        -ExpectedInspectionId $resolvedInspectionId `
        -ChromeRoot $target.element
    $detailAlreadyVisible = [bool]$beforeDetailState.confirmed

    $changed = $false
    $target2 = if ($detailAlreadyVisible) { $target } else { $null }
    $document2 = if ($detailAlreadyVisible) { $document } else { $null }
    $afterSnapshot = if ($detailAlreadyVisible) { $before } else { $null }
    $detailState = if ($detailAlreadyVisible) { $beforeDetailState } else { $null }
    $activationMethod = if ($detailAlreadyVisible) { "already_visible" } else { $null }
    $activationAttempts = @()

    # A UIA pattern saying it ran is only an activation attempt, never proof
    # that ADAS Map navigated. Try each bounded strategy and independently
    # require the exact inspection detail modal/structure to appear.
    if (-not $detailAlreadyVisible) {
      foreach ($method in @("legacy_accessible", "invoke_pattern", "dpi_aware_mouse", "focused_enter")) {
        Bring-To-Front -Target $target | Out-Null
        Start-Sleep -Milliseconds 120

        $attempted = $false
        if ($method -eq "legacy_accessible") {
            $attempted = Invoke-LegacyDefaultAction -Element $view
        }
        elseif ($method -eq "invoke_pattern") {
            $attempted = Invoke-InvokePatternOnly -Element $view
        }
        elseif ($method -eq "dpi_aware_mouse") {
            $attempted = Click-ElementCenter -Element $view
        }
        elseif ($method -eq "focused_enter") {
            $attempted = Invoke-FocusedEnter -Element $view
        }

        $activationAttempts += [pscustomobject]@{
            method = $method
            attempted = $attempted
        }
        if (-not $attempted) { continue }

        for ($poll = 0; $poll -lt 10; $poll++) {
            Start-Sleep -Milliseconds 300
            $windows2 = @(Get-Chrome-Windows)
            $target2 = Select-AdasMap-Window -Windows $windows2
            if ($null -eq $target2) { continue }

            $document2 = Find-Web-Document -ChromeRoot $target2.element
            if ($null -eq $document2) { continue }

            $afterSnapshot = Document-Snapshot -Document $document2
            $afterSignature = Snapshot-Signature -Snapshot $afterSnapshot
            $detailState = Detail-State `
                -Document $document2 `
                -ExpectedInspectionId $resolvedInspectionId `
                -ChromeRoot $target2.element

            if (
                $afterSignature -and
                $afterSignature -ne $beforeSignature -and
                $detailState.confirmed
            ) {
                $changed = $true
                $activationMethod = $method
                break
            }
        }
        if ($changed) { break }
      }
    }

    if ((-not $changed -and -not $detailAlreadyVisible) -or $null -eq $document2) {
        $anyAttempted = @($activationAttempts | Where-Object { $_.attempted }).Count -gt 0
        [pscustomobject]@{
            success = $false
            action = "details"
            status = if ($anyAttempted) { "view_did_not_navigate" } else { "view_click_failed" }
            ro_number = $RoNumber
            vin = $current.vin
            vehicle = $current.vehicle
            inspection_id = $resolvedInspectionId
            row_binding_confirmed = $current.row_binding_confirmed
            modal_inspection_confirmed = if ($detailState) { $detailState.modal_inspection_confirmed } else { $false }
            view_control = $viewData
            row_expansion = $rowExpansion
            activation_attempts = $activationAttempts
            detail_state = $detailState
            before_values = @($before.values | Select-Object -First 180)
            after_values = if ($afterSnapshot) {
                @($afterSnapshot.values | Select-Object -First 180)
            } else { @() }
        } | ConvertTo-Json -Depth 9 -Compress
        exit 0
    }

    Bring-To-Front -Target $target2 | Out-Null
    $modalForSelection = Resolve-Detail-Modal `
        -Document $document2 `
        -ExpectedInspectionId $resolvedInspectionId
    if (-not $modalForSelection.confirmed) {
        [pscustomobject]@{
            success = $false
            action = "details"
            status = "view_did_not_navigate"
            ro_number = $RoNumber
            vin = $current.vin
            vehicle = $current.vehicle
            inspection_id = $resolvedInspectionId
            row_binding_confirmed = $current.row_binding_confirmed
            modal_inspection_confirmed = $false
            modal_status = $modalForSelection.status
            row_expansion = $rowExpansion
            activation_method = $activationMethod
            activation_attempts = $activationAttempts
        } | ConvertTo-Json -Depth 10 -Compress
        exit 0
    }
    $requiredSelection = Ensure-Required-Tab -Document $modalForSelection.root
    if (-not $requiredSelection.confirmed) {
        [pscustomobject]@{
            success = $false
            action = "details"
            status = "requirements_unparsed"
            ro_number = $RoNumber
            vin = $current.vin
            vehicle = $current.vehicle
            inspection_id = $resolvedInspectionId
            row_binding_confirmed = $current.row_binding_confirmed
            modal_inspection_confirmed = $detailState.modal_inspection_confirmed
            document_changed = $changed
            detail_already_visible = $detailAlreadyVisible
            detail_confirmed = $detailState.confirmed
            required_context = $requiredSelection
            row_expansion = $rowExpansion
            activation_method = $activationMethod
            activation_attempts = $activationAttempts
        } | ConvertTo-Json -Depth 10 -Compress
        exit 0
    }
    $detailState = Detail-State `
        -Document $document2 `
        -ExpectedInspectionId $resolvedInspectionId `
        -ChromeRoot $target2.element
    $afterSnapshot = $detailState.snapshot
    $values = @($afterSnapshot.values)
    $vin = Extract-Vin-From-Values -Values $values
    if (-not $vin) { $vin = $current.vin }

    if (-not $detailState.requirements_parse_confident) {
        [pscustomobject]@{
            success = $false
            action = "details"
            status = "requirements_unparsed"
            ro_number = $RoNumber
            vin = $vin
            vehicle = $current.vehicle
            inspection_id = $resolvedInspectionId
            document_changed = $changed
            detail_already_visible = $detailAlreadyVisible
            detail_confirmed = $detailState.confirmed
            required_context_confirmed = $detailState.required_context_confirmed
            required_region_confirmed = $detailState.required_region_confirmed
            modal_inspection_confirmed = $detailState.modal_inspection_confirmed
            modal_runtime_id = $detailState.modal_runtime_id
            row_binding_confirmed = $current.row_binding_confirmed
            row_expansion = $rowExpansion
            activation_method = $activationMethod
            activation_attempts = $activationAttempts
            values = @($values | Select-Object -First 260)
        } | ConvertTo-Json -Depth 10 -Compress
        exit 0
    }

    $requirements = @($detailState.requirements)
    $calibrations = @($requirements | ForEach-Object { $_.label })

    # Return the captured modal evidence before dismissing it. The Python
    # WorkChrome adapter always follows this action with close-details and
    # refuses to report success unless that close is proven. The optional
    # legacy download-report action also performs its own close after capture.

    [pscustomobject]@{
        success = $true
        action = "details"
        status = "details_visible"
        ro_number = $RoNumber
        target_title = $target2.title
        document_changed = $changed
        detail_already_visible = $detailAlreadyVisible
        detail_confirmed = $detailState.confirmed
        modal_inspection_confirmed = $detailState.modal_inspection_confirmed
        modal_runtime_id = $detailState.modal_runtime_id
        required_context_confirmed = $detailState.required_context_confirmed
        required_region_confirmed = $detailState.required_region_confirmed
        row_binding_confirmed = $current.row_binding_confirmed
        row_expansion = $rowExpansion
        required_tab_activated = $requiredSelection.activated
        activation_method = $activationMethod
        activation_attempts = $activationAttempts
        vin = $vin
        vehicle = $current.vehicle
        inspection_id = $resolvedInspectionId
        requirements_parse_confident = $detailState.requirements_parse_confident
        explicit_no_calibration = $detailState.explicit_no_calibration
        requirements = $requirements
        calibration_candidates = $calibrations
        source_url = $detailState.source_url
        report_links = $detailState.report_links
        alldata_links = $detailState.alldata_links
        values = @($values | Select-Object -First 260)
    } | ConvertTo-Json -Depth 9 -Compress
    exit 0
}

if ($Action -eq "open-report") {
    # Exploration only: click the "Report" button and observe what happens
    # (new Chrome window/tab, in-page navigation, or neither). Does not save,
    # print, or download anything -- that comes once we know the mechanism.
    $document = Find-Web-Document -ChromeRoot $target.element
    if ($null -eq $document) {
        [pscustomobject]@{
            success = $false
            action = "open-report"
            status = "web_document_not_found"
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    $windowsBefore = @(Get-Chrome-Windows | ForEach-Object {
        [pscustomobject]@{ title = $_.title; process_id = $_.process_id; handle = $_.handle }
    })
    $before = Document-Snapshot -Document $document
    $beforeSignature = Snapshot-Signature -Snapshot $before

    $reportButton = Find-Visible-ExactName-CaseSensitive -Root $document -Name "Report"
    if ($null -eq $reportButton) {
        [pscustomobject]@{
            success = $false
            action = "open-report"
            status = "report_button_not_found"
            page_values = @($before.values | Select-Object -First 200)
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    $reportData = Element-Data -Element $reportButton

    $activation_method = "legacy_accessible"
    $opened = Invoke-LegacyDefaultAction -Element $reportButton
    if (-not $opened) {
        $activation_method = "dpi_aware_mouse"
        $opened = Click-ElementCenter -Element $reportButton
    }

    Start-Sleep -Milliseconds 900

    $windowsAfter = @(Get-Chrome-Windows | ForEach-Object {
        [pscustomobject]@{ title = $_.title; process_id = $_.process_id; handle = $_.handle }
    })

    $document2 = Find-Web-Document -ChromeRoot $target.element
    $afterSnapshot = if ($null -ne $document2) { Document-Snapshot -Document $document2 } else { $null }
    $afterSignature = if ($null -ne $afterSnapshot) { Snapshot-Signature -Snapshot $afterSnapshot } else { "" }

    [pscustomobject]@{
        success = $true
        action = "open-report"
        status = "report_clicked"
        activation_method = $activation_method
        click_reported_success = $opened
        report_control = $reportData
        window_count_before = $windowsBefore.Count
        window_count_after = $windowsAfter.Count
        windows_before = $windowsBefore
        windows_after = $windowsAfter
        document_signature_changed = ($afterSignature -ne $beforeSignature)
        page_values_after = if ($afterSnapshot) { @($afterSnapshot.values | Select-Object -First 220) } else { @() }
    } | ConvertTo-Json -Depth 9 -Compress
    exit 0
}

if ($Action -eq "open-oe-link") {
    # Exploration only, same contract as open-report: assumes the detail
    # modal is already open (call `details` first), clicks one requirement
    # row's own custom-link element -- the per-requirement OE Reference
    # link that Requirement-Records already reads the label from but never
    # invokes -- and observes what happens (new Chrome window/tab, in-page
    # navigation, or neither). Does not save, download, or close anything
    # it opens -- that comes once we know the mechanism.
    if ([string]::IsNullOrWhiteSpace($RequirementLabel)) {
        throw "open-oe-link requires -RequirementLabel"
    }

    $document = Find-Web-Document -ChromeRoot $target.element
    if ($null -eq $document) {
        [pscustomobject]@{
            success = $false
            action = "open-oe-link"
            status = "web_document_not_found"
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    $windowsBefore = @(Get-Chrome-Windows | ForEach-Object {
        [pscustomobject]@{ title = $_.title; process_id = $_.process_id; handle = $_.handle }
    })
    $before = Document-Snapshot -Document $document
    $beforeSignature = Snapshot-Signature -Snapshot $before

    # Requirement rows use the site's custom-link class (same detection
    # Requirement-Records uses). Deeper links revealed inside an expanded
    # row -- e.g. the "Informational Links" panel's named ALLDATA links --
    # are not styled that way, so this falls back to any element exposing
    # InvokePattern (button or real hyperlink) once the strict class match
    # comes up empty. Deliberately not reusing Requirement-Records itself,
    # since that function only returns computed data (label/link/row_values),
    # never the live element a click needs.
    $normalizedTarget = ([string]$RequirementLabel).ToLowerInvariant() -replace '[^a-z0-9]+', ''
    $linkElement = $null
    $candidateLabels = @()
    $fallbackCandidateLabels = @()
    foreach ($element in (Descendants -Root $document)) {
        try {
            if ($element.Current.IsOffscreen) { continue }
            $isCustomLink = [string]$element.Current.ClassName -match '(?i)(?:^|\s)custom-link(?:\s|$)'
            $label = ""
            try { $label = (([string]$element.Current.Name -split '\s+' | Where-Object { $_ }) -join " ").Trim() } catch {}
            if ([string]::IsNullOrWhiteSpace($label)) { continue }
            $normalizedLabel = $label.ToLowerInvariant() -replace '[^a-z0-9]+', ''
            if ($isCustomLink) {
                $candidateLabels += $label
                if ($normalizedLabel -eq $normalizedTarget) {
                    $linkElement = $element
                    break
                }
                continue
            }
            $invokable = $false
            try {
                $invokable = $null -ne $element.GetCurrentPattern(
                    [System.Windows.Automation.InvokePattern]::Pattern
                )
            }
            catch {}
            if (-not $invokable) { continue }
            $fallbackCandidateLabels += $label
            if ($normalizedLabel -eq $normalizedTarget) {
                $linkElement = $element
            }
        }
        catch {}
    }

    if ($null -eq $linkElement) {
        [pscustomobject]@{
            success = $false
            action = "open-oe-link"
            status = "requirement_link_not_found"
            requirement_label = $RequirementLabel
            candidate_labels = @($candidateLabels | Select-Object -Unique | Select-Object -First 60)
            fallback_candidate_labels = @($fallbackCandidateLabels | Select-Object -Unique | Select-Object -First 60)
        } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }

    $linkData = Element-Data -Element $linkElement

    $activation_method = "legacy_accessible"
    $opened = Invoke-LegacyDefaultAction -Element $linkElement
    if (-not $opened) {
        $activation_method = "dpi_aware_mouse"
        $opened = Click-ElementCenter -Element $linkElement
    }

    Start-Sleep -Milliseconds 900

    $windowsAfter = @(Get-Chrome-Windows | ForEach-Object {
        [pscustomobject]@{ title = $_.title; process_id = $_.process_id; handle = $_.handle }
    })

    $document2 = Find-Web-Document -ChromeRoot $target.element
    $afterSnapshot = if ($null -ne $document2) { Document-Snapshot -Document $document2 } else { $null }
    $afterSignature = if ($null -ne $afterSnapshot) { Snapshot-Signature -Snapshot $afterSnapshot } else { "" }

    [pscustomobject]@{
        success = $true
        action = "open-oe-link"
        status = "requirement_link_clicked"
        requirement_label = $RequirementLabel
        activation_method = $activation_method
        click_reported_success = $opened
        link_control = $linkData
        window_count_before = $windowsBefore.Count
        window_count_after = $windowsAfter.Count
        windows_before = $windowsBefore
        windows_after = $windowsAfter
        document_signature_changed = ($afterSignature -ne $beforeSignature)
        page_values_after = if ($afterSnapshot) { @($afterSnapshot.values | Select-Object -First 220) } else { @() }
    } | ConvertTo-Json -Depth 9 -Compress
    exit 0
}

if ($Action -eq "capture-oe-si") {
    # Full chain, proven by hand step-by-step first: assumes the detail modal
    # is already open (call `details` first). Clicks one requirement's own
    # link, then the ALLDATA "...Calibration" link it reveals (never
    # "Caution" -- deliberately excluded, matching what Otis actually wants
    # kept), which navigates this same Chrome window into the exact OEM
    # procedure page in ALLDATA. Prints that page to PDF via Chrome's own
    # Ctrl+P (not ALLDATA's own print icon -- that one has no discoverable
    # accessible name and needing a human to click it once already proved
    # unreliable to automate; Ctrl+P is a universal, always-available
    # Chrome shortcut and reaches the same native print dialog). Saves the
    # PDF using the same click-to-focus-then-type approach already proven
    # reliable for this exact class of Views-drawn dialog in
    # Invoke-Download-Report, since ValuePattern/SetFocus report success on
    # these controls without doing anything real.
    if ([string]::IsNullOrWhiteSpace($RequirementLabel)) {
        throw "capture-oe-si requires -RequirementLabel"
    }
    if ([string]::IsNullOrWhiteSpace($SavePath)) {
        throw "capture-oe-si requires -SavePath"
    }
    if (Test-Path -LiteralPath $SavePath) {
        [pscustomobject]@{
            success = $false
            action = "capture-oe-si"
            status = "target_already_exists"
            save_path = $SavePath
        } | ConvertTo-Json -Depth 6 -Compress
        exit 0
    }

    function Normalize-Label([string]$Value) {
        return ($Value.ToLowerInvariant() -replace '[^a-z0-9]+', '')
    }

    $document = Find-Web-Document -ChromeRoot $target.element
    if ($null -eq $document) {
        [pscustomobject]@{
            success = $false
            action = "capture-oe-si"
            status = "web_document_not_found"
        } | ConvertTo-Json -Depth 6 -Compress
        exit 0
    }

    # Step 1: click the requirement's own custom-link row (same detection as
    # Requirement-Records / open-oe-link).
    $normalizedTarget = Normalize-Label $RequirementLabel
    $reqElement = $null
    $reqCandidates = @()
    foreach ($element in (Descendants -Root $document)) {
        try {
            if ($element.Current.IsOffscreen) { continue }
            if ([string]$element.Current.ClassName -notmatch '(?i)(?:^|\s)custom-link(?:\s|$)') { continue }
            $label = ""
            try { $label = (([string]$element.Current.Name -split '\s+' | Where-Object { $_ }) -join " ").Trim() } catch {}
            if ([string]::IsNullOrWhiteSpace($label)) { continue }
            $reqCandidates += $label
            if ((Normalize-Label $label) -eq $normalizedTarget) {
                $reqElement = $element
                break
            }
        }
        catch {}
    }
    if ($null -eq $reqElement) {
        [pscustomobject]@{
            success = $false
            action = "capture-oe-si"
            status = "requirement_link_not_found"
            requirement_label = $RequirementLabel
            candidate_labels = @($reqCandidates | Select-Object -Unique | Select-Object -First 60)
        } | ConvertTo-Json -Depth 6 -Compress
        exit 0
    }
    # Captured before the click: once any requirement is clicked, every
    # group's Informational Links panel can end up simultaneously present
    # in the DOM (observed directly -- a second click while a prior group's
    # panel was still rendered returned that stale panel's link instead of
    # this row's own). The clicked row's own vertical position is a stable
    # anchor -- its own accordion group's links render at or below it, the
    # next group's row lower still -- so scoping the search to "closest
    # match at or below this exact row" survives that leftover state
    # instead of just grabbing the first ALLDATA link anywhere on the page.
    $reqTop = $reqElement.Current.BoundingRectangle.Top
    $reqClicked = Invoke-LegacyDefaultAction -Element $reqElement
    if (-not $reqClicked) { $reqClicked = Click-ElementCenter -Element $reqElement }
    if (-not $reqClicked) {
        [pscustomobject]@{
            success = $false
            action = "capture-oe-si"
            status = "requirement_link_click_failed"
            requirement_label = $RequirementLabel
        } | ConvertTo-Json -Depth 6 -Compress
        exit 0
    }
    Start-Sleep -Milliseconds 900

    # Step 2: find the "Calibration Procedure - ..." informational link the
    # click revealed -- never "Caution". Not every one of these mentions
    # ALLDATA by name (observed directly: a Rearview Camera's own link read
    # "Calibration Procedure - Rearview Camera System Inspection", no
    # "ALLDATA" anywhere in it, while EyeSight's said "ALLDATA Service
    # Information" -- ALLDATA branding is not a reliable signal). The
    # stable prefix across every example seen is "Calibration Procedure -",
    # which also excludes unrelated same-page buttons like "Create
    # Calibration" that merely contain the word.
    #
    # From a genuinely fresh modal (no prior click in this same session)
    # exactly one non-Caution match exists and its position relative to the
    # clicked row is not reliable -- observed both above and below it
    # directly, so requiring "below" as a precondition rejected a correct,
    # unambiguous match. Position is only useful to break a tie: it becomes
    # meaningful exactly when there is more than one candidate, which only
    # happens when an earlier click in the same modal left a prior group's
    # panel still rendered (observed directly). So: one match -> trust it;
    # more than one -> prefer whichever is closest below the clicked row,
    # falling back to closest overall if none are below it.
    $document2 = Find-Web-Document -ChromeRoot $target.element
    $searchRoot = if ($null -ne $document2) { $document2 } else { $document }
    $alldataCandidates = @()
    $matchCandidates = @()
    foreach ($element in (Descendants -Root $searchRoot)) {
        try {
            if ($element.Current.IsOffscreen) { continue }
            $name = [string]$element.Current.Name
            # No fixed suffix format: seen with a "- family name" suffix,
            # seen completely bare with nothing after it at all. Match the
            # stable leading phrase only.
            if ($name -notmatch '(?i)^\s*Calibration Procedure\b') { continue }
            $invokable = $false
            try {
                $invokable = $null -ne $element.GetCurrentPattern(
                    [System.Windows.Automation.InvokePattern]::Pattern
                )
            }
            catch {}
            if (-not $invokable) { continue }
            $alldataCandidates += $name
            if ($name -match '(?i)caution') { continue }
            $top = $element.Current.BoundingRectangle.Top
            $matchCandidates += [pscustomobject]@{ element = $element; name = $name; top = $top }
        }
        catch {}
    }
    # Do not guess which single link is "the right one" -- observed directly
    # that a wrong guess here silently saved unrelated content as if it were
    # correct. Otis's call: capture every distinct candidate under this
    # requirement and let downstream classification in X sort out which one
    # actually documents it, rather than gambling on one at capture time.
    if ($matchCandidates.Count -eq 0) {
        [pscustomobject]@{
            success = $false
            action = "capture-oe-si"
            status = "alldata_calibration_link_not_found"
            requirement_label = $RequirementLabel
            requirement_row_top = $reqTop
            alldata_candidates = @($alldataCandidates | Select-Object -Unique | Select-Object -First 40)
        } | ConvertTo-Json -Depth 6 -Compress
        exit 0
    }
    $linksToCapture = @($matchCandidates | Sort-Object top)
    $multiCandidate = $linksToCapture.Count -gt 1
    $pathExt = [IO.Path]::GetExtension($SavePath)
    $pathStem = $SavePath.Substring(0, $SavePath.Length - $pathExt.Length)

    $captureResults = @()
    $candidateIndex = 0
    foreach ($candidate in $linksToCapture) {
        $candidateIndex++
        $candidateSavePath = if ($multiCandidate) { "${pathStem}_${candidateIndex}${pathExt}" } else { $SavePath }
        $calibrationLinkName = $candidate.name

        if (Test-Path -LiteralPath $candidateSavePath) {
            $captureResults += [pscustomobject]@{
                success = $true
                status = "target_already_exists"
                requirement_label = $RequirementLabel
                alldata_link = $calibrationLinkName
                save_path = $candidateSavePath
            }
            continue
        }

        # A previous candidate in this same loop returns focus to the ADAS
        # tab at the end of its own iteration, so re-bring it to front
        # before clicking the next candidate's link -- and, live diagnostics
        # proved, the FIRST candidate needs exactly the same treatment: this
        # used to only run from the second candidate onward, trusting
        # whatever brought the window forward minutes earlier (a separate
        # lookup/details process call) to still hold by click time. It
        # doesn't reliably -- confirmed live, both configured click methods
        # (LegacyIAccessiblePattern and a raw coordinate click) report
        # success while producing no navigation at all, no popup-blocked
        # indicator, nothing -- consistent with the physical click landing
        # somewhere other than this window because it wasn't actually
        # foreground at that moment.
        $adasWindowsNow = @(Get-Chrome-Windows)
        $adasNow = Select-AdasMap-Window -Windows $adasWindowsNow
        $broughtToFront = $null
        if ($null -ne $adasNow) {
            $broughtToFront = Bring-To-Front -Target $adasNow
            Start-Sleep -Milliseconds 300
        }

        # Step 3: confirm this window actually navigated to ALLDATA. Live
        # diagnostics (added after most captures were observed dying here)
        # proved LegacyIAccessiblePattern.DoDefaultAction() reports success
        # (does not throw) on this exact link even when it demonstrably
        # causes no navigation at all -- window title unchanged, no new
        # ALLDATA-titled window anywhere -- so a truthy return from it is
        # not trustworthy proof of a real click for this control. Try each
        # click method in turn and verify by OBSERVED EFFECT (a shorter
        # poll per method) rather than trusting either method's own return
        # value; only fall through to failure once every method has been
        # tried and none produced a confirmed navigation.
        $navigatedWindow = $null
        $notFoundSeen = $false
        $hasRealContent = $false
        $clickMethodUsed = $null
        $popupBlockedSeen = $false
        $clickAttemptDiagnostics = @()
        foreach ($clickMethod in @("legacy_accessible", "element_center")) {
            $elementStillValid = $false
            $elementOffscreen = $null
            $elementRect = $null
            try {
                $elementOffscreen = $candidate.element.Current.IsOffscreen
                $r = $candidate.element.Current.BoundingRectangle
                $elementRect = "$($r.Left),$($r.Top),$($r.Width),$($r.Height)"
                $elementStillValid = $true
            }
            catch {}

            $clicked = if ($clickMethod -eq "legacy_accessible") {
                Invoke-LegacyDefaultAction -Element $candidate.element
            } else {
                Click-ElementCenter -Element $candidate.element
            }
            $clickAttemptDiagnostics += [pscustomobject]@{
                method = $clickMethod
                clicked = $clicked
                element_still_valid = $elementStillValid
                element_offscreen = $elementOffscreen
                element_rect = $elementRect
                brought_to_front = $broughtToFront
            }
            if (-not $clicked) { continue }

            # Diagnostic-only: a window.open() popup Chrome silently blocks
            # looks, from every signal this function otherwise checks,
            # identical to a click that did nothing -- same handle, same
            # title, no new window. Chrome marks a blocked popup with an
            # omnibox icon/toolbar text; catch it once, right after the
            # click and before the real navigation poll below, while it's
            # still fresh.
            Start-Sleep -Milliseconds 350
            try {
                foreach ($element in (Descendants -Root $target.element)) {
                    if ($element.Current.IsOffscreen) { continue }
                    if ([string]$element.Current.Name -match '(?i)pop.?up.*blocked|blocked.*pop.?up') {
                        $popupBlockedSeen = $true
                        break
                    }
                }
            }
            catch {}

            for ($p = 0; $p -lt 8; $p++) {
                Start-Sleep -Milliseconds 400
                $windowsAfterNav = @(Get-Chrome-Windows)
                $navigatedWindow = $windowsAfterNav | Where-Object { $_.handle -eq $target.handle } | Select-Object -First 1
                if ($null -eq $navigatedWindow -or [string]$navigatedWindow.title -notmatch '(?i)alldata') { continue }
                foreach ($element in (Descendants -Root $navigatedWindow.element)) {
                    try {
                        if ($element.Current.IsOffscreen) { continue }
                        if ([string]$element.Current.Name -match '(?i)article not found') { $notFoundSeen = $true; break }
                        if (
                            $element.Current.ControlType -eq [System.Windows.Automation.ControlType]::Text -and
                            ([string]$element.Current.Name).Trim().Length -gt 40
                        ) { $hasRealContent = $true }
                    }
                    catch {}
                }
                if ($notFoundSeen -or $hasRealContent) { break }
            }
            if ($navigatedWindow -and [string]$navigatedWindow.title -match '(?i)alldata') {
                $clickMethodUsed = $clickMethod
                break
            }
        }
        if ($null -eq $navigatedWindow -or [string]$navigatedWindow.title -notmatch '(?i)alldata') {
            # Diagnostic-only: this is by far the most common capture
            # failure and, from the outside, looks identical whether (a) no
            # navigation happened at all, (b) it navigated but under a
            # different top-level handle than $target's (a new window, not
            # a same-window tab), or (c) it navigated on the right handle
            # but the title/content just hadn't settled inside the poll
            # window. Recording every open window's own title+handle at the
            # moment we give up lets that be told apart from the log alone
            # instead of guessing blind.
            $allWindowsNow = @(Get-Chrome-Windows)
            $anyAlldataTitled = @($allWindowsNow | Where-Object { [string]$_.title -match '(?i)alldata' })
            $captureResults += [pscustomobject]@{
                success = $false
                status = "alldata_navigation_not_confirmed"
                requirement_label = $RequirementLabel
                alldata_link = $calibrationLinkName
                observed_title = if ($navigatedWindow) { $navigatedWindow.title } else { $null }
                target_handle = $target.handle
                open_windows = @($allWindowsNow | ForEach-Object { [pscustomobject]@{ title = $_.title; handle = $_.handle } })
                alldata_titled_window_under_different_handle = ($anyAlldataTitled.Count -gt 0)
                click_attempts = $clickAttemptDiagnostics
                popup_blocked_indicator_seen = $popupBlockedSeen
            }
            Close-Alldata-Tab-And-Return-To-Adas -Handle $target.handle | Out-Null
            continue
        }
        $alldataTitle = $navigatedWindow.title

        # ADAS Map's own cross-reference to ALLDATA can be stale even when
        # the click and navigation both mechanically succeed -- a real link
        # can lead to ALLDATA's own "Article Not Found" error page. Refuse
        # to print/save it.
        if ($notFoundSeen) {
            $captureResults += [pscustomobject]@{
                success = $false
                status = "alldata_article_not_found"
                requirement_label = $RequirementLabel
                alldata_link = $calibrationLinkName
                alldata_title = $alldataTitle
            }
            Close-Alldata-Tab-And-Return-To-Adas -Handle $target.handle | Out-Null
            continue
        }

        # Step 4: Chrome's own Ctrl+P, not ALLDATA's unlabeled print icon.
        Bring-To-Front -Target $navigatedWindow | Out-Null
        Start-Sleep -Milliseconds 400
        [System.Windows.Forms.SendKeys]::SendWait("^p")

        $printDialogFound = $false
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Milliseconds 400
            foreach ($element in (Descendants -Root $navigatedWindow.element)) {
                try {
                    if ($element.Current.IsOffscreen) { continue }
                    if ([string]$element.Current.Name -eq "Destination") {
                        $printDialogFound = $true
                        break
                    }
                }
                catch {}
            }
            if ($printDialogFound) { break }
        }
        if (-not $printDialogFound) {
            $captureResults += [pscustomobject]@{
                success = $false
                status = "print_dialog_not_found"
                requirement_label = $RequirementLabel
                alldata_link = $calibrationLinkName
                alldata_title = $alldataTitle
            }
            Close-Alldata-Tab-And-Return-To-Adas -Handle $target.handle | Out-Null
            continue
        }

        # Step 5: verify the destination is already "Microsoft Print to PDF"
        # (Chrome remembers the last-used destination; proven true by hand).
        # Fail closed rather than trying to drive the destination picker blind
        # if it somehow is not.
        $destinationCombo = $null
        foreach ($element in (Descendants -Root $navigatedWindow.element)) {
            try {
                if ($element.Current.IsOffscreen) { continue }
                if (
                    $element.Current.Name -eq "Destination" -and
                    $element.Current.ControlType -eq [System.Windows.Automation.ControlType]::ComboBox
                ) {
                    $destinationCombo = $element
                    break
                }
            }
            catch {}
        }
        $destinationValue = $null
        if ($null -ne $destinationCombo) {
            try {
                $destinationValue = (
                    $destinationCombo.GetCurrentPattern(
                        [System.Windows.Automation.ValuePattern]::Pattern
                    )
                ).Current.Value
            }
            catch {}
        }
        if ($destinationValue -notmatch '(?i)Microsoft Print to PDF') {
            $captureResults += [pscustomobject]@{
                success = $false
                status = "destination_not_pdf"
                requirement_label = $RequirementLabel
                alldata_link = $calibrationLinkName
                observed_destination = $destinationValue
                alldata_title = $alldataTitle
            }
            Close-Alldata-Tab-And-Return-To-Adas -Handle $target.handle | Out-Null
            continue
        }

        # Step 6: click the print preview's own action button (labelled "Print"
        # for the Microsoft Print to PDF virtual printer, proven by hand).
        $actionButton = $null
        foreach ($element in (Descendants -Root $navigatedWindow.element)) {
            try {
                if ($element.Current.IsOffscreen) { continue }
                if ([string]$element.Current.ClassName -eq "action-button") {
                    $actionButton = $element
                    break
                }
            }
            catch {}
        }
        if ($null -eq $actionButton) {
            $captureResults += [pscustomobject]@{
                success = $false
                status = "print_action_button_not_found"
                requirement_label = $RequirementLabel
                alldata_link = $calibrationLinkName
                alldata_title = $alldataTitle
            }
            Close-Alldata-Tab-And-Return-To-Adas -Handle $target.handle | Out-Null
            continue
        }
        $printClicked = Click-ElementCenter -Element $actionButton
        if (-not $printClicked) {
            $captureResults += [pscustomobject]@{
                success = $false
                status = "print_action_click_failed"
                requirement_label = $RequirementLabel
                alldata_link = $calibrationLinkName
                alldata_title = $alldataTitle
            }
            Close-Alldata-Tab-And-Return-To-Adas -Handle $target.handle | Out-Null
            continue
        }

        # Step 7: the resulting Windows "Save Print Output As" dialog -- same
        # nested-Views-dialog shape Invoke-Download-Report already handles.
        $dialog = $null
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 300
            $dialog = Find-Save-Dialog -ChromeRoot $navigatedWindow.element -ChromeProcessId $navigatedWindow.process_id
            if ($null -ne $dialog) { break }
        }
        if ($null -eq $dialog) {
            $captureResults += [pscustomobject]@{
                success = $false
                status = "save_dialog_not_found"
                requirement_label = $RequirementLabel
                alldata_link = $calibrationLinkName
                alldata_title = $alldataTitle
            }
            Close-Alldata-Tab-And-Return-To-Adas -Handle $target.handle | Out-Null
            continue
        }

        $desc = @(Descendants -Root $dialog.element)
        $fileNameBox = $desc | Where-Object {
            -not $_.Current.IsOffscreen -and [string]$_.Current.AutomationId -eq "1001"
        } | Select-Object -First 1
        if ($null -eq $fileNameBox) {
            $label = $desc | Where-Object {
                -not $_.Current.IsOffscreen -and
                [string]$_.Current.Name -match "(?i)^\s*File name:?\s*$"
            } | Select-Object -First 1
            if ($null -ne $label) {
                $labelRect = $label.Current.BoundingRectangle
                $labelRightEdge = $labelRect.Left + $labelRect.Width
                $fileNameBox = @(
                    $desc |
                    Where-Object {
                        -not $_.Current.IsOffscreen -and
                        [Math]::Abs($_.Current.BoundingRectangle.Top - $labelRect.Top) -lt 10 -and
                        $_.Current.BoundingRectangle.Left -ge $labelRightEdge -and
                        [string]$_.Current.Name -ne "File name:"
                    } |
                    Sort-Object { $_.Current.BoundingRectangle.Left }
                ) | Select-Object -First 1
            }
        }
        if ($null -eq $fileNameBox) {
            $captureResults += [pscustomobject]@{
                success = $false
                status = "save_dialog_filename_box_not_found"
                requirement_label = $RequirementLabel
                alldata_link = $calibrationLinkName
                dialog_title = $dialog.title
            }
            Close-Alldata-Tab-And-Return-To-Adas -Handle $target.handle | Out-Null
            continue
        }

        $focused = Click-ElementCenter -Element $fileNameBox
        if (-not $focused) {
            $captureResults += [pscustomobject]@{
                success = $false
                status = "save_dialog_filename_focus_failed"
                requirement_label = $RequirementLabel
                alldata_link = $calibrationLinkName
                dialog_title = $dialog.title
            }
            Close-Alldata-Tab-And-Return-To-Adas -Handle $target.handle | Out-Null
            continue
        }
        Start-Sleep -Milliseconds 250
        [System.Windows.Forms.SendKeys]::SendWait("^a")
        Start-Sleep -Milliseconds 100
        [System.Windows.Forms.SendKeys]::SendWait("{DEL}")
        Start-Sleep -Milliseconds 150
        [System.Windows.Forms.SendKeys]::SendWait($candidateSavePath)
        Start-Sleep -Milliseconds 300

        $saveButton = @(
            $desc |
            Where-Object {
                -not $_.Current.IsOffscreen -and
                [string]$_.Current.Name -match "(?i)^\s*&?Save\s*$"
            }
        ) | Select-Object -First 1
        if ($null -eq $saveButton) {
            $captureResults += [pscustomobject]@{
                success = $false
                status = "save_dialog_save_button_not_found"
                requirement_label = $RequirementLabel
                alldata_link = $calibrationLinkName
                dialog_title = $dialog.title
            }
            Close-Alldata-Tab-And-Return-To-Adas -Handle $target.handle | Out-Null
            continue
        }
        $saveClicked = Click-ElementCenter -Element $saveButton
        if (-not $saveClicked) {
            $saveClicked = Invoke-LegacyDefaultAction -Element $saveButton
        }
        if (-not $saveClicked) {
            try { [System.Windows.Forms.SendKeys]::SendWait("{ENTER}") } catch {}
        }

        # A multi-page PDF write can pause mid-flush long enough to look
        # "stable" between two checks and still not be finished -- observed
        # directly (a real capture settled at a false-stable 1MB plateau before
        # continuing on to its true ~6MB size). Three consecutive equal reads,
        # not two, before declaring done.
        $savedOk = $false
        $finalSize = 0
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Milliseconds 300
            if (-not (Test-Path -LiteralPath $candidateSavePath)) { continue }
            try {
                $sizeA = (Get-Item -LiteralPath $candidateSavePath).Length
                Start-Sleep -Milliseconds 600
                $sizeB = (Get-Item -LiteralPath $candidateSavePath).Length
                if ($sizeA -le 0 -or $sizeA -ne $sizeB) { continue }
                Start-Sleep -Milliseconds 600
                $sizeC = (Get-Item -LiteralPath $candidateSavePath).Length
                if ($sizeC -eq $sizeB) {
                    $savedOk = $true
                    $finalSize = $sizeC
                    break
                }
            }
            catch {}
        }

        # The ALLDATA click opens a genuine new tab and focuses it -- the
        # original ADAS Map tab is not closed or navigated away, just left
        # inactive (Chrome can even memory-discard it while it waits, observed
        # directly). Re-navigating this window by URL instead would hit a fresh
        # login page and risk the shared authenticated session -- proven the
        # wrong move by hand. Switching back via Chrome's own tab search to the
        # original tab is what actually works and never touches credentials.
        $returnedThisCandidate = Close-Alldata-Tab-And-Return-To-Adas -Handle $target.handle

        $captureResults += [pscustomobject]@{
            success = $savedOk
            status = if ($savedOk) { "oe_si_saved" } else { "oe_si_not_confirmed_saved" }
            requirement_label = $RequirementLabel
            alldata_link = $calibrationLinkName
            alldata_title = $alldataTitle
            save_path = $candidateSavePath
            file_size = $finalSize
            returned_to_adas_map = $returnedThisCandidate
        }
    }

    # Otis caught this live (with a screenshot): clicking a requirement row
    # opens a genuine modal popup titled "Informational Links" (its own X
    # and Cancel button) -- not an inline accordion panel as assumed. It
    # was never being closed, so it sat open into the next requirement's
    # turn, and that's exactly the "prior group's panel still rendered"
    # condition the link-selection code above already knew could cause
    # bleed-through -- except now every subsequent requirement re-discovers
    # and re-downloads this same leftover modal's links on top of its own,
    # compounding with each requirement processed, and one capture actually
    # hung the whole run waiting on a save dialog behind it. Close the
    # modal itself via its own Cancel button before returning a result.
    try {
        $adasWindowsFinal = @(Get-Chrome-Windows)
        $adasFinal = Select-AdasMap-Window -Windows $adasWindowsFinal
        if ($null -ne $adasFinal) {
            Bring-To-Front -Target $adasFinal | Out-Null
            Start-Sleep -Milliseconds 300
            $infoLinksModalOpen = $null -ne ((Descendants -Root $adasFinal.element) | Where-Object {
                -not $_.Current.IsOffscreen -and [string]$_.Current.Name -eq "Informational Links"
            } | Select-Object -First 1)
            if ($infoLinksModalOpen) {
                $modalCancelBtn = (Descendants -Root $adasFinal.element) | Where-Object {
                    -not $_.Current.IsOffscreen -and [string]$_.Current.Name -eq "Cancel"
                } | Select-Object -First 1
                if ($null -ne $modalCancelBtn) {
                    Click-ElementCenter -Element $modalCancelBtn | Out-Null
                    Start-Sleep -Milliseconds 500
                }
                else {
                    try { [System.Windows.Forms.SendKeys]::SendWait("{ESC}") } catch {}
                    Start-Sleep -Milliseconds 400
                }
            }
        }
    }
    catch {}

    [pscustomobject]@{
        action = "capture-oe-si"
        requirement_label = $RequirementLabel
        candidate_count = $linksToCapture.Count
        results = $captureResults
    } | ConvertTo-Json -Depth 8 -Compress
    exit 0
}

function Invoke-Download-Report {
    param(
        $Target,
        [string]$RoNumber,
        [string]$SavePath,
        [string]$ExpectedInspectionId
    )

    if ([string]::IsNullOrWhiteSpace($ExpectedInspectionId)) {
        return @{ success = $false; action = "download-report"; status = "inspection_id_missing" }
    }

    if (Test-Path -LiteralPath $SavePath) {
        return @{
            success = $false
            action = "download-report"
            status = "target_already_exists"
            save_path = $SavePath
        }
    }

    $target = $Target
    $chromeProcessId = $target.process_id

    # A Save dialog may already be open from a prior attempt (Chrome's own
    # nested Views dialog persists across script invocations since it lives
    # in the live browser, not in this process). Reuse it instead of
    # re-clicking Download, which would stack a second dialog on top.
    $dialog = Find-Save-Dialog -ChromeRoot $target.element -ChromeProcessId $chromeProcessId

    if ($null -eq $dialog) {
        $document = Find-Web-Document -ChromeRoot $target.element
        if ($null -eq $document) {
            return @{ success = $false; action = "download-report"; status = "web_document_not_found" }
        }

        $detailState = Detail-State `
            -Document $document `
            -ExpectedInspectionId $ExpectedInspectionId `
            -ChromeRoot $target.element
        if (-not $detailState.confirmed) {
            return @{
                success = $false
                action = "download-report"
                status = "inspection_mismatch"
                inspection_id = $ExpectedInspectionId
                detail_state = $detailState
            }
        }

        # The PDF viewer toolbar (Download/Print/etc.) lives in Chrome's own UI
        # around the embedded PDF plugin, not inside the SPA's web Document, so
        # it must be located against the whole window rather than $document.
        $downloadButton = Find-Visible-ExactName-CaseSensitive -Root $target.element -Name "Download"

        if ($null -eq $downloadButton) {
            # Report view is not open yet -- open it first, then look again.
            $reportButton = Find-Visible-ExactName-CaseSensitive -Root $document -Name "Report"
            if ($null -eq $reportButton) {
                return @{ success = $false; action = "download-report"; status = "report_button_not_found" }
            }

            $reportOpened = Invoke-LegacyDefaultAction -Element $reportButton
            if (-not $reportOpened) {
                $reportOpened = Click-ElementCenter -Element $reportButton
            }

            # The PDF viewer's toolbar takes longer to render for larger
            # reports (more calibration items -> more PDF pages/content), so
            # a single fixed wait is not reliable -- poll for it instead.
            $downloadButton = $null
            for ($i = 0; $i -lt 10 -and $null -eq $downloadButton; $i++) {
                Start-Sleep -Milliseconds 500
                $downloadButton = Find-Visible-ExactName-CaseSensitive -Root $target.element -Name "Download"
            }
            if ($null -eq $downloadButton) {
                return @{ success = $false; action = "download-report"; status = "download_button_not_found"; report_opened = $reportOpened }
            }
        }

        $downloadClicked = Invoke-LegacyDefaultAction -Element $downloadButton
        if (-not $downloadClicked) {
            $downloadClicked = Click-ElementCenter -Element $downloadButton
        }

        if (-not $downloadClicked) {
            return @{ success = $false; action = "download-report"; status = "download_click_failed" }
        }

        # Wait for the Save dialog to appear -- either a real top-level common
        # dialog, or Chrome's own nested Views-toolkit dialog (see Find-Save-Dialog).
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Milliseconds 250
            $dialog = Find-Save-Dialog -ChromeRoot $target.element -ChromeProcessId $chromeProcessId
            if ($null -ne $dialog) { break }
        }

        if ($null -eq $dialog) {
            return @{
                success = $false
                action = "download-report"
                status = "save_dialog_not_found"
                message = "Download did not produce a Save dialog within the timeout. Chrome may be configured to auto-save to a default folder instead."
            }
        }
    }

    $desc = @(Descendants -Root $dialog.element)

    # Chrome's own Views-toolkit Save dialog exposes the filename field and
    # the Save/Cancel controls as generic ControlType.Pane elements (not real
    # Edit/Button controls), so control-type filtering alone can't find them.
    # Empirically this dialog's filename value pane has a stable
    # AutomationId of "1001", unique within the dialog's own descendant
    # scope (a same-valued AutomationId exists elsewhere in Chrome's chrome,
    # e.g. the address bar area, but that is a sibling, not nested under
    # this dialog, so scoping to $desc keeps it unambiguous).
    $fileNameBox = $desc | Where-Object {
        -not $_.Current.IsOffscreen -and [string]$_.Current.AutomationId -eq "1001"
    } | Select-Object -First 1

    if ($null -eq $fileNameBox) {
        # Fall back to a label-relative search: the file list's own inline
        # rename boxes (Name/Date modified/Type/Size columns) can share a
        # similar vertical position, so pick the candidate whose left edge
        # sits closest to (not merely to the right of) the label -- the
        # nearest neighbor, not the furthest one in the row.
        $label = $desc | Where-Object {
            -not $_.Current.IsOffscreen -and
            [string]$_.Current.Name -match "(?i)^\s*File name:?\s*$"
        } | Select-Object -First 1

        if ($null -ne $label) {
            $labelRect = $label.Current.BoundingRectangle
            $labelRightEdge = $labelRect.Left + $labelRect.Width
            $fileNameBox = @(
                $desc |
                Where-Object {
                    -not $_.Current.IsOffscreen -and
                    [Math]::Abs($_.Current.BoundingRectangle.Top - $labelRect.Top) -lt 10 -and
                    $_.Current.BoundingRectangle.Left -ge $labelRightEdge -and
                    [string]$_.Current.Name -ne "File name:"
                } |
                Sort-Object { $_.Current.BoundingRectangle.Left }
            ) | Select-Object -First 1
        }
    }

    if ($null -eq $fileNameBox) {
        # Fall back to a conventional Edit/ComboBox filename control, in case
        # this turned out to be a real native common dialog after all.
        foreach ($el in $desc) {
            try {
                if ($el.Current.IsOffscreen -or -not $el.Current.IsEnabled) { continue }
                $type = $el.Current.ControlType
                if (
                    $type -eq [System.Windows.Automation.ControlType]::Edit -or
                    $type -eq [System.Windows.Automation.ControlType]::ComboBox
                ) {
                    $fileNameBox = $el
                    break
                }
            }
            catch {}
        }
    }

    if ($null -eq $fileNameBox) {
        $dialogControls = @(Visible-Control-Data -Root $dialog.element -Limit 100)
        return @{
            success = $false
            action = "download-report"
            status = "save_dialog_filename_box_not_found"
            dialog_title = $dialog.title
            dialog_nested = $dialog.nested
            dialog_controls = $dialogControls
        }
    }

    # This dialog's filename field is a custom Views control, not a real
    # Edit box. ValuePattern.SetValue()/SetFocus() have both been observed
    # to report success without producing any real effect on it -- only a
    # genuine physical click reliably focuses it. Click first, then clear
    # and type, rather than routing through Set-Element-Value's
    # pattern-based attempts.
    $focused = Click-ElementCenter -Element $fileNameBox
    if (-not $focused) {
        return @{ success = $false; action = "download-report"; status = "save_dialog_filename_set_failed"; dialog_title = $dialog.title }
    }
    Start-Sleep -Milliseconds 250
    [System.Windows.Forms.SendKeys]::SendWait("^a")
    Start-Sleep -Milliseconds 100
    [System.Windows.Forms.SendKeys]::SendWait("{DEL}")
    Start-Sleep -Milliseconds 150
    [System.Windows.Forms.SendKeys]::SendWait($SavePath)
    Start-Sleep -Milliseconds 300

    $saveButton = @(
        $desc |
        Where-Object {
            -not $_.Current.IsOffscreen -and
            [string]$_.Current.Name -match "(?i)^\s*&?Save\s*$"
        }
    ) | Select-Object -First 1

    if ($null -eq $saveButton) {
        return @{ success = $false; action = "download-report"; status = "save_dialog_save_button_not_found"; dialog_title = $dialog.title }
    }

    # Same reasoning as the filename field: prefer the physical click that
    # is proven to work on these Views-drawn Pane controls over
    # LegacyIAccessible, which can report success without clicking anything.
    $saveClicked = Click-ElementCenter -Element $saveButton
    if (-not $saveClicked) {
        $saveClicked = Invoke-LegacyDefaultAction -Element $saveButton
    }
    if (-not $saveClicked) {
        try { [System.Windows.Forms.SendKeys]::SendWait("{ENTER}") } catch {}
    }

    $savedOk = $false
    $finalSize = 0
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 300
        if (Test-Path -LiteralPath $SavePath) {
            try {
                $size1 = (Get-Item -LiteralPath $SavePath).Length
                Start-Sleep -Milliseconds 400
                $size2 = (Get-Item -LiteralPath $SavePath).Length
                if ($size1 -gt 0 -and $size1 -eq $size2) {
                    $savedOk = $true
                    $finalSize = $size2
                    break
                }
            }
            catch {}
        }
    }

    return @{
        success = $savedOk
        action = "download-report"
        status = if ($savedOk) { "report_saved" } else { "report_not_confirmed_saved" }
        ro_number = $RoNumber
        save_path = $SavePath
        file_size = $finalSize
    }
}

if ($Action -eq "download-report") {
    if ([string]::IsNullOrWhiteSpace($RoNumber)) {
        throw "download-report requires -RoNumber"
    }
    if ([string]::IsNullOrWhiteSpace($SavePath)) {
        throw "download-report requires -SavePath"
    }
    if ([string]::IsNullOrWhiteSpace($InspectionId)) {
        throw "download-report requires -InspectionId"
    }

    $result = Invoke-Download-Report `
        -Target $target `
        -RoNumber $RoNumber `
        -SavePath $SavePath `
        -ExpectedInspectionId $InspectionId

    # Always attempt to close the inspection modal, win or lose, so the
    # browser is left clean for whatever RO gets looked up next. This is a
    # best-effort no-op if there was never a modal open to begin with.
    #
    # Deliberately scoped to the whole window, not Find-Web-Document's
    # scope: with the PDF viewer open, its embedded document is now the
    # largest on-screen Document element, so Find-Web-Document resolves to
    # the PDF's own content -- which never contains the outer modal's
    # custom-close icon -- instead of the app shell around it.
    $modalClose = Close-Inspection-Modal -Root $target.element
    $result["modal_close"] = $modalClose

    $result | ConvertTo-Json -Depth 9 -Compress
    exit 0
}

}
catch {
    [pscustomobject]@{
        success = $false
        action = $Action
        status = "bridge_error"
        message = $_.Exception.Message
        exception = $_.Exception.GetType().Name
    } | ConvertTo-Json -Depth 7 -Compress
    exit 0
}
