
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "work-chrome-adas-map.ps1"

def value():
    return SCRIPT.read_text(encoding="utf-8")

def test_no_bad_string_join():
    text = value()
    assert '" ".Join(' not in text
    assert '-join " "' in text


def test_script_parses_with_windows_powershell():
    command = (
        "$tokens=$null;$errors=$null;"
        f"[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',"
        "[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors.Count){$errors|%{$_.Message};exit 1}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

def test_document_scoped_scraping():
    text = value()
    assert "function Find-Web-Document" in text
    assert "ControlType]::Document" in text
    assert "Document-Snapshot" in text
    assert "view_did_not_navigate" in text
    assert "browser chrome was not inspected" in text
    assert "function Chrome-Address-AdasMap-Url" in text
    assert "Test-AdasMap-Url -Value $value -OpusOnly" in text
    assert 'return $hostName -eq "opus.adasmap.com"' in text
    assert '$scope = if ($null -ne $document) { $document } else { $target.element }' not in text
    assert "Read-Current-Ro -Root $target.element" not in text


def test_details_uses_bounded_activation_and_authoritative_navigation_proof():
    text = value()
    assert "function Click-ElementCenter" in text
    assert "function Invoke-InvokePatternOnly" in text
    assert "function Invoke-FocusedEnter" in text
    assert '@("legacy_accessible", "invoke_pattern", "dpi_aware_mouse", "focused_enter")' in text
    assert "function Detail-State" in text
    assert "$detailState.confirmed" in text
    assert "$afterSignature -ne $beforeSignature" in text
    assert 'status = if ($anyAttempted) { "view_did_not_navigate" } else { "view_click_failed" }' in text


def test_bridge_is_dpi_aware_and_uses_legacy_default_action():
    text = value()
    assert "SetProcessDpiAwarenessContext" in text
    assert "Invoke-LegacyDefaultAction" in text
    assert "LegacyIAccessiblePattern" in text
    assert "Find-Best-View-Control" in text
    assert "dpi_aware_mouse" in text


def test_view_is_bound_to_exact_ro_and_inspection_without_first_view_fallback():
    text = value()
    assert "function Find-Exact-Ro-Hits" in text
    assert "function Deepest-Common-Ancestor" in text
    assert "function Inspection-Row-Association" in text
    assert "function Resolve-Inspection-View" in text
    assert "function Inspection-Ids-In-Scope" in text
    assert "-ExactRow $best.row" in text
    assert "-VehicleRow $ExactRow" in text
    assert "Find-Exact-Ro-Hits -Root $ExactRow" in text
    assert "Where-Object { $_.inspection_id -and $_.row_binding_confirmed }" in text
    assert "row_binding_confirmed = [bool]$resolution.row_binding_confirmed" in text
    assert "ExpectedInspectionId" in text
    assert 'status = "ambiguous_inspection"' in text
    assert 'status = "inspection_mismatch"' in text
    assert "$viewCandidates[0]" not in text
    assert "ControlType]::DataItem" in text


def test_required_parser_uses_first_custom_link_per_detail_list_item():
    text = value()
    assert "function Required-Tab-State" in text
    assert "function Ensure-Required-Tab" in text
    assert "function Resolve-Detail-Modal" in text
    assert "Deepest-Common-Ancestor" in text
    assert "-ExcludedRoot $Document" in text
    assert "Find-View-Candidates -Root $common" in text
    assert "modal_inspection_confirmed = $true" in text
    assert "required_tab_structure_ambiguous" in text
    assert "required_tab_selection_unconfirmed" in text
    assert "btn-success" in text
    assert "$itemRect.Top -lt ($regionTop - 2)" in text
    assert "($itemRect.Top + $itemRect.Height) -gt ($regionBottom + 2)" in text
    assert "function Requirement-Records" in text
    assert "-Root $modalRoot" in text
    assert "Descendants -Root $Root" in text
    assert "ControlType]::ListItem" in text
    assert "custom-link" in text
    assert "$requirementControl = $customLinks[0]" in text
    assert 'source_context = "selected_required_modal"' in text
    assert "source_context_runtime_id = $ModalRuntimeId" in text
    assert "required_region_confirmed = $requiredRegionConfirmed" in text
    assert 'status = "requirements_unparsed"' in text
    assert "Calibration-Candidates -Values" not in text
    assert "function Calibration-Candidates" not in text


def test_sort_object_mixed_direction_syntax_is_valid_shape():
    text = value()
    assert "Sort-Object score -Descending, area" not in text
    assert '@{ Expression = "score"; Descending = $true }' in text
    assert '@{ Expression = "area"; Ascending = $true }' in text


def test_view_lookup_does_not_require_enabled_bit():
    text = value()
    assert "Chrome often exposes web text cells with IsEnabled=false" in text
    assert "view_candidates_seen" in text


def test_exact_row_vehicle_identity_is_parsed_propagated_and_fail_closed():
    text = value()
    assert "function Vehicle-From-Row-Values" in text
    assert "model_configuration = $modelConfiguration" in text
    assert "vehicle = $current.vehicle" in text
    assert "vehicle = $after.vehicle" in text
    assert 'status = "vehicle_identity_unparsed"' in text


def test_lookup_propagates_portal_observed_shop_separately_from_requested_shop():
    text = value()
    assert "function Get-Selected-Business-Text" in text
    assert "$observedShop = Get-Selected-Business-Text -Root $scope" in text
    assert "observed_shop = $observedShop" in text


def test_shop_switch_proves_toolbar_label_and_retries_grid_search_boundedly():
    text = value()
    assert "$centerY -ge ($searchRect.Top - 8)" in text
    assert "$centerY -le ($searchRect.Top + $searchRect.Height + 8)" in text
    assert "selection_confirmed = ($after -eq $TargetName)" in text
    select_business = text[text.index("function Select-Business") : text.index("\ntry {", text.index("function Select-Business"))]
    assert select_business.index("Click-ElementCenter -Element $match") < select_business.index(
        "Invoke-LegacyDefaultAction -Element $match"
    )
    assert "function Wait-For-Stable-Ro-Grid" in text
    assert "$stableResolvedSamples -ge 3" in text
    assert "$maxSearchAttempts = if ($shopSwitch -and $shopSwitch.changed) { 2 } else { 1 }" in text
    assert "search_attempts = $searchAttempts" in text


def test_duplicate_ro_rows_use_only_complete_unique_safe_vehicle_hint():
    text = value()
    assert "[int]$ExpectedYear = 0" in text
    assert "[string]$ExpectedMake" in text
    assert "[string]$ExpectedModel" in text
    assert "function Test-Vehicle-Hints" in text
    assert "$candidateRows.Count -gt 1" in text
    assert "$completeHint" in text
    assert "$candidateRows.Count -eq 0" in text
    assert 'resolution_status = "vehicle_identity_mismatch"' in text
    assert "$candidateRows.Count -ne 1" in text
    assert 'resolution_status = "ambiguous_ro"' in text
    assert "(?:\\.\\.\\.|…)" in text


def test_vehicle_hint_filter_matches_literal_stage2_duplicate_shapes():
    command = (
        "$tokens=$null;$errors=$null;"
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',"
        "[ref]$tokens,[ref]$errors);"
        "$fn=$ast.Find({param($node)"
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and "
        "$node.Name -eq 'Test-Vehicle-Hints'},$true);"
        "Invoke-Expression $fn.Extent.Text;"
        "$exact=[pscustomobject]@{year=2020;make='Nissan';model_configuration='Murano SL FWD'};"
        "$other=[pscustomobject]@{year=2020;make='Nissan';model_configuration='Murano SL'};"
        "$truncated=[pscustomobject]@{year=2023;make='Ford';model_configuration='Maverick XLT AWD SuperCrew'};"
        "if(-not (Test-Vehicle-Hints -Vehicle $exact -Year 2020 -Make 'NISSAN' -Model 'Murano SL FWD')){exit 1};"
        "if(Test-Vehicle-Hints -Vehicle $other -Year 2020 -Make 'NISSAN' -Model 'Murano SL FWD'){exit 2};"
        "if(-not (Test-Vehicle-Hints -Vehicle $truncated -Year 2023 -Make 'FORD' -Model 'Maverick XLT ...')){exit 3};"
        "if(Test-Vehicle-Hints -Vehicle $exact -Year 2020 -Make 'NISSAN' -Model ''){exit 4}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_unique_hinted_row_expands_only_its_descendant_edit_and_reproves_view():
    text = value()
    assert "function Resolve-Exact-Row-EditLeaf" in text
    assert "Descendants -Root $Row" in text
    assert "ControlType]::Hyperlink" in text
    assert "(?i)^\\s*edit\\s*$" in text
    assert 'status = "row_edit_ambiguous"' in text
    assert "function Expand-Proven-Vehicle-Row" in text
    assert "-not $InitialCurrent.vehicle_hint_applied" in text
    assert '$InitialCurrent.resolution_status -notin @("view_not_found", "inspection_id_missing")' in text
    assert '@("invoke_pattern", "dpi_aware_mouse")' in text
    assert "Never reuse a potentially stale row/edit element" in text
    assert "Test-Same-Observed-Vehicle-Row" in text
    assert "$refreshed.row_binding_confirmed" in text
    assert "$null -ne $refreshed.view_element" in text
    assert 'status = "post_expand_view_unproven"' in text
    assert "row_expansion = $rowExpansion" in text


def test_post_expand_identity_check_requires_same_vin_vehicle_and_unique_hint():
    command = (
        "$tokens=$null;$errors=$null;"
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',"
        "[ref]$tokens,[ref]$errors);"
        "$fn=$ast.Find({param($node)"
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and "
        "$node.Name -eq 'Test-Same-Observed-Vehicle-Row'},$true);"
        "Invoke-Expression $fn.Extent.Text;"
        "$vehicle=[pscustomobject]@{year=2020;make='Nissan';model_configuration='Murano SL FWD'};"
        "$before=[pscustomobject]@{found=$true;vehicle_hint_applied=$true;vin='5N1AZ2CJ7LN175052';vehicle=$vehicle};"
        "$same=[pscustomobject]@{found=$true;vehicle_hint_applied=$true;vin='5N1AZ2CJ7LN175052';vehicle=$vehicle};"
        "$wrongVin=[pscustomobject]@{found=$true;vehicle_hint_applied=$true;vin='5N1AZ2CJ7LN175505';vehicle=$vehicle};"
        "$unhinted=[pscustomobject]@{found=$true;vehicle_hint_applied=$false;vin='5N1AZ2CJ7LN175052';vehicle=$vehicle};"
        "if(-not (Test-Same-Observed-Vehicle-Row -ExpectedCurrent $before -ActualCurrent $same)){exit 1};"
        "if(Test-Same-Observed-Vehicle-Row -ExpectedCurrent $before -ActualCurrent $wrongVin){exit 2};"
        "if(Test-Same-Observed-Vehicle-Row -ExpectedCurrent $before -ActualCurrent $unhinted){exit 3}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
