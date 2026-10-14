#  hammer_build_systems.py
#  Class containing all the methods to create VLSI flow build system infrastructure
#
#  See LICENSE for licence details.

from .driver import HammerDriver

import getpass
import os
import datetime
import shutil
import hashlib
import re
import sys
import textwrap
from typing import List, Dict, Tuple, Callable, Optional

#import pdb
#pdb.set_trace()

def build_noop(driver: HammerDriver, append_error_func: Callable[[str], None]) -> dict:
    """
    Do nothing, just return the dependency graph.

    :param driver: The HammerDriver
    :return: The dependency graph
    """
    dependency_graph = driver.get_hierarchical_dependency_graph()
    return dependency_graph


def build_makefile(driver: HammerDriver, append_error_func: Callable[[str], None],
                   hammer_exec: Optional[str] = None) -> dict:
    #pdb.set_trace()    #[Trace Path 1]
    """
    Build a Makefile include in the obj_dir called hammer.d. This is intended to be dynamically
    created and included into a top-level Makefile.

    The Makefile will contain targets for the following hammer actions, as well as any necessary
    bridge actions (xyz-to-abc):
        - pcb
        - syn
        - par
        - drc
        - lvs
        - sim-rtl
        - sim-syn
        - sim-par
        - power-rtl
        - power-syn
        - power-par
        - formal-syn
        - formal-par
        - timing-syn
        - timing-par

    For hierarchical flows, the syn, par, drc, lvs, sim, power, formal, and timing actions will all be suffixed with the name
    of the hierarchical modules (e.g. syn-Top, syn-SubModA, par-SubModA, etc.). The appropriate
    dependencies and bridge actions are automatically generated from the hierarchy provided in the
    Hammer IR.

    For actions that can be run at multiple points in the flow such as sim, the name of the target
    will include the action it is being run after (e.g. sim-syn, sim-par, etc.). With no suffix
    an rtl level simulation will be run.

    Additionally, "redo" steps are created (e.g. redo-par for flat designs or redo-par-Top for
    hierarchical), which allow the user to bypass the normal Makefile dependencies and force a
    rerun of a particular task. This is useful when the user wants to change an input Hammer IR file
    knowing it will not affect intermediate steps in the design.

    An example use case for integrating this file into a top flow is provided below. Be sure to use
    real tabs if copying this snippet!

    ```
    TOP ?= MyTop
    OBJ_DIR ?= $(abspath build-$(TOP))
    INPUT_CONFS ?= foo.yaml bar.yaml baz.yaml

    HAMMER_EXEC ?= ./mychip-vlsi.py

    .PHONY: all
    all: drc-$(TOP) lvs-$(TOP)

    GENERATED_CONF = $(OBJ_DIR)/input.yaml

    $(GENERATED_CONF):
        echo "synthesis.inputs.top_module: $(TOP)" > $@
        echo "pcb.inputs.top_module: $(TOP)" >> $@

    $(OBJ_DIR)/hammer.d: $(GENERATED_CONF)
        $(HAMMER_EXEC) -e env.yaml $(foreach x,$(INPUT_CONFS) $(GENERATED_CONF), -p $(x)) --obj_dir $(OBJ_DIR) build

    include $(OBJ_DIR)/hammer.d
    ```

    The generated Makefile has a few variables that are set if absent. This allows the user to override them without
    modifying hammer.d. They are listed as follows:
        - HAMMER_EXEC: This sets the actual python executable containing the HammerDriver main() function. It is set to
          the executable used to generate the Makefile by default.
        - HAMMER_DEPENDENCIES: The list of dependences to use for the initial syn and pcb targets. It is set to the set
          of all input configurations, environment settings, and input files by default.
        - HAMMER_*_DEPENDENCIES: There is a version of this variable for each action. It allows the user to have more
          fine grained depencies compared to the blunt HAMMER_DEPENDENCIES. It is set as a dependency for the respective
          action. Often used with clearing the global HAMMER_DEPENDENCIES.
        - HAMMER_EXTRA_ARGS: This is passed to the Hammer executable for all targets. This is unset by default.
          Its primary uses are for adding additional configuration files with -p, --to_step/until_step, and/or --from_step/
          after_step options. An example use is "make redo-par-Top HAMMER_EXTRA_ARGS="-p patch.yaml --from_step placement".

    :param driver: The HammerDriver
    :param hammer_exec: The Hammer driver the targets run. Defaults to the executable running this build.
    :return: The dependency graph
    """
    dependency_graph = driver.get_hierarchical_dependency_graph()
    #pdb.set_trace()
    makefile = os.path.join(driver.obj_dir, "hammer.d")
    default_dependencies = driver.options.project_configs + driver.options.environment_configs
    default_dependencies.extend(list(driver.database.get_setting("synthesis.inputs.input_files", [])))
    # Resolve the canonical path for each dependency
    default_dependencies = [os.path.realpath(x) for x in default_dependencies]
    output = "HAMMER_EXEC ?= {}\n".format(os.path.realpath(hammer_exec or sys.argv[0]))
    output += "HAMMER_DEPENDENCIES ?= {}\n\n".format(" ".join(default_dependencies))
    syn_deps = "$(HAMMER_DEPENDENCIES)"
    # Get the confs passed into this execution
    env_confs = " ".join(["-e " + os.path.realpath(x) for x in driver.options.environment_configs])
    proj_confs = " ".join(["-p " + os.path.realpath(x) for x in driver.options.project_configs])
    obj_dir = os.path.realpath(driver.obj_dir)

    # Global steps that are the same for hier or flat
    pcb_run_dir = os.path.join(obj_dir, "pcb-rundir")
    pcb_out = os.path.join(pcb_run_dir, "pcb-output-full.json")
    output += textwrap.dedent("""
        ####################################################################################
        ## Global steps temp
        ####################################################################################
        .PHONY: pcb
        pcb: {pcb_out}

        {pcb_out}: {syn_deps}
        \t$(HAMMER_EXEC) {env_confs} {all_inputs} --obj_dir {obj_dir} pcb

        """.format(pcb_out=pcb_out, syn_deps=syn_deps, env_confs=env_confs, all_inputs=proj_confs, obj_dir=obj_dir))

    make_text = textwrap.dedent("""
        ####################################################################################
        ## Steps for {mod}
        ####################################################################################
        .PHONY: sim-rtl{suffix} syn{suffix} syn-to-sim{suffix} sim-syn{suffix} syn-to-par{suffix} par{suffix} par-to-sim{suffix} sim-par{suffix} sim-par-to-power{suffix} par-to-power{suffix} power-par{suffix} power-rtl{suffix} sim-rtl-to-power{suffix} sim-syn-to-power{suffix} syn-to-power{suffix} power-syn{suffix} par-to-drc{suffix} drc{suffix} par-to-lvs{suffix} lvs{suffix} syn-to-formal{suffix} formal-syn{suffix} par-to-formal{suffix} formal-par{suffix} syn-to-timing{suffix} timing-syn{suffix} par-to-timing{suffix} timing-par{suffix}

        sim-rtl{suffix}          : {sim_rtl_out}
        syn{suffix}              : {syn_out}

        syn-to-sim{suffix}       : {sim_syn_in}
        sim-syn{suffix}          : {sim_syn_out}

        syn-to-par{suffix}       : {par_in}
        par{suffix}              : {par_out}

        par-to-sim{suffix}       : {sim_par_in}
        sim-par{suffix}          : {sim_par_out}

        sim-par-to-power{suffix} : {power_sim_par_in}
        par-to-power{suffix}     : {power_par_in}
        power-par{suffix}        : {power_par_out}

        sim-rtl-to-power{suffix} : {power_sim_rtl_in}
        power-rtl{suffix}        : {power_rtl_out}

        sim-syn-to-power{suffix} : {power_sim_syn_in}
        syn-to-power{suffix}     : {power_syn_in}
        power-syn{suffix}        : {power_syn_out}

        par-to-drc{suffix}       : {drc_in}
        drc{suffix}              : {drc_out}

        par-to-lvs{suffix}       : {lvs_in}
        lvs{suffix}              : {lvs_out}

        syn-to-formal{suffix}    : {formal_syn_in}
        formal-syn{suffix}       : {formal_syn_out}

        par-to-formal{suffix}    : {formal_par_in}
        formal-par{suffix}       : {formal_par_out}

        syn-to-timing{suffix}    : {timing_syn_in}
        timing-syn{suffix}       : {timing_syn_out}

        par-to-timing{suffix}    : {timing_par_in}
        timing-par{suffix}       : {timing_par_out}

        {par_to_syn}

        {sim_rtl_out}: {syn_deps} $(HAMMER_SIM_RTL_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} {p_sim_rtl_in} $(HAMMER_EXTRA_ARGS) --sim_rundir {sim_rtl_run_dir} --obj_dir {obj_dir} sim{suffix}

        {power_sim_rtl_in}: {sim_rtl_out}
        \t$(HAMMER_EXEC) {env_confs} -p {sim_rtl_out} $(HAMMER_EXTRA_ARGS) -o {power_sim_rtl_in} --obj_dir {obj_dir} sim-to-power

        {power_rtl_out}: {power_sim_rtl_in} $(HAMMER_POWER_RTL_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {power_sim_rtl_in} $(HAMMER_EXTRA_ARGS) --power_rundir {power_rtl_run_dir} --obj_dir {obj_dir} power{suffix}

        {syn_out}: {syn_deps} $(HAMMER_SYN_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} {p_syn_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} syn{suffix}

        {sim_syn_in}: {syn_out}
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {sim_syn_in} --obj_dir {obj_dir} syn-to-sim

        {sim_syn_out}: {sim_syn_in} $(HAMMER_SIM_SYN_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {sim_syn_in} $(HAMMER_EXTRA_ARGS) --sim_rundir {sim_syn_run_dir} --obj_dir {obj_dir} sim{suffix}

        {power_sim_syn_in}: {sim_syn_out}
        \t$(HAMMER_EXEC) {env_confs} -p {sim_syn_out} $(HAMMER_EXTRA_ARGS) -o {power_sim_syn_in} --obj_dir {obj_dir} sim-to-power

        {power_syn_in}: {syn_out}
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {power_syn_in} --obj_dir {obj_dir} syn-to-power

        {power_syn_out}: {power_sim_syn_in} {power_syn_in} $(HAMMER_POWER_SYN_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {power_sim_syn_in} -p {power_syn_in} $(HAMMER_EXTRA_ARGS) --power_rundir {power_syn_run_dir} --obj_dir {obj_dir} power{suffix}

        {par_in}: {syn_out}
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {par_in} --obj_dir {obj_dir} syn-to-par

        {par_out}: {par_in} $(HAMMER_PAR_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {par_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} par{suffix}

        {sim_par_in}: {par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {sim_par_in} --obj_dir {obj_dir} par-to-sim

        {sim_par_out}: {sim_par_in} $(HAMMER_SIM_PAR_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {sim_par_in} $(HAMMER_EXTRA_ARGS) --sim_rundir {sim_par_run_dir} --obj_dir {obj_dir} sim{suffix}

        {power_sim_par_in}: {sim_par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {sim_par_out} $(HAMMER_EXTRA_ARGS) -o {power_sim_par_in} --obj_dir {obj_dir} sim-to-power

        {power_par_in}: {par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {power_par_in} --obj_dir {obj_dir} par-to-power

        {power_par_out}: {power_sim_par_in} {power_par_in} $(HAMMER_POWER_PAR_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {power_sim_par_in} -p {power_par_in} $(HAMMER_EXTRA_ARGS) --power_rundir {power_par_run_dir} --obj_dir {obj_dir} power{suffix}

        {drc_in}: {par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {drc_in} --obj_dir {obj_dir} par-to-drc

        {drc_out}: {drc_in} $(HAMMER_DRC_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {drc_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} drc{suffix}

        {lvs_in}: {par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {lvs_in} --obj_dir {obj_dir} par-to-lvs

        {lvs_out}: {lvs_in} $(HAMMER_LVS_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {lvs_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} lvs{suffix}

        {formal_syn_in}: {syn_out}
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {formal_syn_in} --obj_dir {obj_dir} syn-to-formal

        {formal_syn_out}: {formal_syn_in} $(HAMMER_FORMAL_SYN_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {formal_syn_in} $(HAMMER_EXTRA_ARGS) --formal_rundir {formal_syn_run_dir} --obj_dir {obj_dir} formal{suffix}

        {formal_par_in}: {par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {formal_par_in} --obj_dir {obj_dir} par-to-formal

        {formal_par_out}: {formal_syn_in} $(HAMMER_FORMAL_PAR_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {formal_par_in} $(HAMMER_EXTRA_ARGS) --formal_rundir {formal_par_run_dir} --obj_dir {obj_dir} formal{suffix}

        {timing_syn_in}: {syn_out}
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {timing_syn_in} --obj_dir {obj_dir} syn-to-timing

        {timing_syn_out}: {timing_syn_in} $(HAMMER_TIMING_SYN_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {timing_syn_in} $(HAMMER_EXTRA_ARGS) --timing_rundir {timing_syn_run_dir} --obj_dir {obj_dir} timing{suffix}

        {timing_par_in}: {par_out}
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {timing_par_in} --obj_dir {obj_dir} par-to-timing

        {timing_par_out}: {timing_syn_in} $(HAMMER_TIMING_PAR_DEPENDENCIES)
        \t$(HAMMER_EXEC) {env_confs} -p {timing_par_in} $(HAMMER_EXTRA_ARGS) --timing_rundir {timing_par_run_dir} --obj_dir {obj_dir} timing{suffix}

        # Redo steps
        # These intentionally break the dependency graph, but allow the flexibility to rerun a step after changing a config.
        # Hammer doesn't know what settings impact synthesis only, e.g., so these are for power-users who "know better."
        # The HAMMER_EXTRA_ARGS variable allows patching in of new configurations with -p or using --to_step or --from_step, for example.
        .PHONY: redo-sim-rtl{suffix} redo-sim-rtl-to-power{suffix} redo-syn{suffix} redo-syn-to-sim{suffix} redo-syn-to-power{suffix} redo-sim-syn{suffix} redo-sim-syn-to-power{suffix} redo-syn-to-par{suffix} redo-par{suffix} redo-par-to-sim{suffix} redo-sim-par{suffix} redo-sim-par-to-power{suffix} redo-par-to-power{suffix} redo-power-par{suffix} redo-par-to-drc{suffix} redo-drc{suffix} redo-par-to-lvs{suffix} redo-lvs{suffix} redo-syn-to-formal{suffix} redo-formal-syn{suffix} redo-par-to-formal{suffix} redo-formal-par{suffix} redo-syn-to-timing{suffix} redo-timing-syn{suffix} redo-par-to-timing{suffix} redo-timing-par{suffix}

        redo-sim-rtl{suffix}:
        \t$(HAMMER_EXEC) {env_confs} {p_sim_rtl_in} $(HAMMER_EXTRA_ARGS) --sim_rundir {sim_rtl_run_dir} --obj_dir {obj_dir} sim{suffix}

        redo-sim-rtl-to-power{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {sim_rtl_out} $(HAMMER_EXTRA_ARGS) -o {power_sim_rtl_in} --obj_dir {obj_dir} sim-to-power

        redo-power-rtl{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {power_sim_rtl_in} $(HAMMER_EXTRA_ARGS) --power_rundir {power_rtl_run_dir} --obj_dir {obj_dir} power{suffix}

        redo-syn{suffix}:
        \t$(HAMMER_EXEC) {env_confs} {p_syn_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} syn{suffix}

        redo-syn-to-sim{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {sim_syn_in} --obj_dir {obj_dir} syn-to-sim

        redo-syn-to-power{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {power_syn_in} --obj_dir {obj_dir} syn-to-power

        redo-sim-syn{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {sim_syn_in} $(HAMMER_EXTRA_ARGS) --sim_rundir {sim_syn_run_dir} --obj_dir {obj_dir} sim{suffix}

        redo-sim-syn-to-power{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {sim_syn_out} $(HAMMER_EXTRA_ARGS) -o {power_sim_syn_in} --obj_dir {obj_dir} sim-to-power

        redo-syn-to-par{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {par_in} --obj_dir {obj_dir} syn-to-par

        redo-power-syn{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {power_sim_syn_in} -p {power_syn_in} $(HAMMER_EXTRA_ARGS) --power_rundir {power_syn_run_dir} --obj_dir {obj_dir} power{suffix}

        redo-par{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} par{suffix}

        redo-par-to-sim{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {sim_par_in} --obj_dir {obj_dir} par-to-sim

        redo-sim-par{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {sim_par_in} $(HAMMER_EXTRA_ARGS) --sim_rundir {sim_par_run_dir} --obj_dir {obj_dir} sim{suffix}

        redo-sim-par-to-power{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {sim_par_out} $(HAMMER_EXTRA_ARGS) -o {power_sim_par_in} --obj_dir {obj_dir} sim-to-power

        redo-par-to-power{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {power_par_in} --obj_dir {obj_dir} par-to-power

        redo-power-par{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {power_sim_par_in} -p {power_par_in} $(HAMMER_EXTRA_ARGS) --power_rundir {power_par_run_dir} --obj_dir {obj_dir} power{suffix}

        redo-par-to-drc{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {drc_in} --obj_dir {obj_dir} par-to-drc

        redo-drc{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {drc_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} drc{suffix}

        redo-par-to-lvs{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {lvs_in} --obj_dir {obj_dir} par-to-lvs

        redo-lvs{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {lvs_in} $(HAMMER_EXTRA_ARGS) --obj_dir {obj_dir} lvs{suffix}

        redo-syn-to-formal{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {formal_syn_in} --obj_dir {obj_dir} syn-to-formal

        redo-formal-syn{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {formal_syn_in} $(HAMMER_EXTRA_ARGS) --formal_rundir {formal_syn_run_dir} --obj_dir {obj_dir} formal{suffix}

        redo-par-to-formal{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {formal_par_in} --obj_dir {obj_dir} par-to-formal

        redo-formal-par{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {formal_par_in} $(HAMMER_EXTRA_ARGS) --formal_rundir {formal_par_run_dir} --obj_dir {obj_dir} formal{suffix}

        redo-syn-to-timing{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {syn_out} $(HAMMER_EXTRA_ARGS) -o {timing_syn_in} --obj_dir {obj_dir} syn-to-timing

        redo-timing-syn{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {timing_syn_in} $(HAMMER_EXTRA_ARGS) --timing_rundir {timing_syn_run_dir} --obj_dir {obj_dir} timing{suffix}

        redo-par-to-timing{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {par_out} $(HAMMER_EXTRA_ARGS) -o {timing_par_in} --obj_dir {obj_dir} par-to-timing

        redo-timing-par{suffix}:
        \t$(HAMMER_EXEC) {env_confs} -p {timing_par_in} $(HAMMER_EXTRA_ARGS) --timing_rundir {timing_par_run_dir} --obj_dir {obj_dir} timing{suffix}

        """)

    if not dependency_graph:
        # Flat flow
        top_module = str(driver.database.get_setting("synthesis.inputs.top_module"))

        # TODO make this DRY
        sim_rtl_run_dir = os.path.join(obj_dir, "sim-rtl-rundir")
        power_rtl_run_dir = os.path.join(obj_dir, "power-rtl-rundir")
        syn_run_dir = os.path.join(obj_dir, "syn-rundir")
        sim_syn_run_dir = os.path.join(obj_dir, "sim-syn-rundir")
        power_syn_run_dir = os.path.join(obj_dir, "power-syn-rundir")
        par_run_dir = os.path.join(obj_dir, "par-rundir")
        sim_par_run_dir = os.path.join(obj_dir, "sim-par-rundir")
        power_par_run_dir = os.path.join(obj_dir, "power-par-rundir")
        drc_run_dir = os.path.join(obj_dir, "drc-rundir")
        lvs_run_dir = os.path.join(obj_dir, "lvs-rundir")
        formal_syn_run_dir = os.path.join(obj_dir, "formal-syn-rundir")
        formal_par_run_dir = os.path.join(obj_dir, "formal-par-rundir")
        timing_syn_run_dir = os.path.join(obj_dir, "timing-syn-rundir")
        timing_par_run_dir = os.path.join(obj_dir, "timing-par-rundir")

        p_sim_rtl_in = proj_confs
        sim_rtl_out = os.path.join(sim_rtl_run_dir, "sim-output-full.json")
        power_sim_rtl_in = os.path.join(obj_dir, "power-sim-rtl-input.json")
        #power_rtl_in = os.path.join(obj_dir, "power-rtl-input.json")
        power_rtl_out = os.path.join(power_rtl_run_dir, "power-output-full.json")
        p_syn_in = proj_confs
        syn_out = os.path.join(syn_run_dir, "syn-output-full.json")
        sim_syn_in = os.path.join(obj_dir, "sim-syn-input.json")
        sim_syn_out = os.path.join(sim_syn_run_dir, "sim-output-full.json")
        power_sim_syn_in = os.path.join(obj_dir, "power-sim-syn-input.json")
        power_syn_in = os.path.join(obj_dir, "power-syn-input.json")
        power_syn_out = os.path.join(power_syn_run_dir, "power-output-full.json")
        par_in = os.path.join(obj_dir, "par-input.json")
        par_out = os.path.join(par_run_dir, "par-output-full.json")
        sim_par_in = os.path.join(obj_dir, "sim-par-input.json")
        sim_par_out = os.path.join(sim_par_run_dir, "sim-output-full.json")
        power_sim_par_in = os.path.join(obj_dir, "power-sim-par-input.json")
        power_par_in = os.path.join(obj_dir, "power-par-input.json")
        power_par_out = os.path.join(power_par_run_dir, "power-output-full.json")
        drc_in = os.path.join(obj_dir, "drc-input.json")
        drc_out = os.path.join(drc_run_dir, "drc-output-full.json")
        lvs_in = os.path.join(obj_dir, "lvs-input.json")
        lvs_out = os.path.join(lvs_run_dir, "lvs-output-full.json")
        formal_syn_in = os.path.join(obj_dir, "formal-syn-input.json")
        formal_syn_out = os.path.join(formal_syn_run_dir, "formal-output-full.json")
        formal_par_in = os.path.join(obj_dir, "formal-par-input.json")
        formal_par_out = os.path.join(formal_par_run_dir, "formal-output-full.json")
        timing_syn_in = os.path.join(obj_dir, "timing-syn-input.json")
        timing_syn_out = os.path.join(timing_syn_run_dir, "timing-output-full.json")
        timing_par_in = os.path.join(obj_dir, "timing-par-input.json")
        timing_par_out = os.path.join(timing_par_run_dir, "timing-output-full.json")

        par_to_syn = ""

        output += make_text.format(suffix="", mod=top_module, env_confs=env_confs, obj_dir=obj_dir, syn_deps=syn_deps,
            par_to_syn=par_to_syn,
            p_sim_rtl_in=p_sim_rtl_in, sim_rtl_out=sim_rtl_out, sim_rtl_run_dir=sim_rtl_run_dir,
            sim_syn_in=sim_syn_in, sim_syn_out=sim_syn_out, sim_syn_run_dir=sim_syn_run_dir,
            power_sim_rtl_in=power_sim_rtl_in, power_rtl_out=power_rtl_out, power_rtl_run_dir=power_rtl_run_dir,
            sim_par_in=sim_par_in, sim_par_out=sim_par_out, sim_par_run_dir=sim_par_run_dir,
            p_syn_in=p_syn_in, syn_out=syn_out, par_in=par_in, par_out=par_out,
            power_sim_syn_in=power_sim_syn_in, power_syn_in=power_syn_in, power_syn_out=power_syn_out, power_syn_run_dir=power_syn_run_dir,
            power_sim_par_in=power_sim_par_in, power_par_in=power_par_in, power_par_out=power_par_out, power_par_run_dir=power_par_run_dir,
            drc_in=drc_in, drc_out=drc_out, lvs_in=lvs_in, lvs_out=lvs_out,
            formal_syn_in=formal_syn_in, formal_syn_out=formal_syn_out, formal_syn_run_dir=formal_syn_run_dir,
            formal_par_in=formal_par_in, formal_par_out=formal_par_out, formal_par_run_dir=formal_par_run_dir,
            timing_syn_in=timing_syn_in, timing_syn_out=timing_syn_out, timing_syn_run_dir=timing_syn_run_dir,
            timing_par_in=timing_par_in, timing_par_out=timing_par_out, timing_par_run_dir=timing_par_run_dir)
    else:
        # Hierarchical flow
        for node, edges in dependency_graph.items():
            out_edges = edges[1]

            # TODO make this DRY
            sim_rtl_run_dir = os.path.join(obj_dir, "sim-rtl-" + node)
            power_rtl_run_dir = os.path.join(obj_dir, "power-rtl-" + node)
            syn_run_dir = os.path.join(obj_dir, "syn-" + node)
            sim_syn_run_dir = os.path.join(obj_dir, "sim-syn-" + node)
            power_syn_run_dir = os.path.join(obj_dir, "power-syn-" + node)
            par_run_dir = os.path.join(obj_dir, "par-" + node)
            sim_par_run_dir = os.path.join(obj_dir, "sim-par-" + node)
            power_par_run_dir = os.path.join(obj_dir, "power-par-" + node)
            drc_run_dir = os.path.join(obj_dir, "drc-" + node)
            lvs_run_dir = os.path.join(obj_dir, "lvs-" + node)
            formal_syn_run_dir = os.path.join(obj_dir, "formal-syn-" + node)
            formal_par_run_dir = os.path.join(obj_dir, "formal-par-" + node)
            timing_syn_run_dir = os.path.join(obj_dir, "timing-syn-" + node)
            timing_par_run_dir = os.path.join(obj_dir, "timing-par-" + node)

            p_sim_rtl_in = proj_confs
            sim_rtl_out = os.path.join(sim_rtl_run_dir, "sim-output-full.json")
            power_sim_rtl_in = os.path.join(obj_dir, "power-sim-rtl-{}-input.json".format(node))
            #power_rtl_in = os.path.join(obj_dir, "power-rtl-{}-input.json".format(node))
            power_rtl_out = os.path.join(power_rtl_run_dir, "power-output-full.json")
            p_syn_in = proj_confs
            syn_out = os.path.join(syn_run_dir, "syn-output-full.json")
            sim_syn_in = os.path.join(obj_dir, "sim-syn-{}-input.json".format(node))
            sim_syn_out = os.path.join(sim_syn_run_dir, "sim-output-full.json")
            power_sim_syn_in = os.path.join(obj_dir, "power-sim-syn-{}-input.json".format(node))
            power_syn_in = os.path.join(obj_dir, "power-syn-{}-input.json".format(node))
            power_syn_out = os.path.join(power_syn_run_dir, "power-output-full.json")
            par_in = os.path.join(obj_dir, "par-{}-input.json".format(node))
            par_out = os.path.join(par_run_dir, "par-output-full.json")
            sim_par_in = os.path.join(obj_dir, "sim-par-{}-input.json".format(node))
            sim_par_out = os.path.join(sim_par_run_dir, "sim-output-full.json")
            power_sim_par_in = os.path.join(obj_dir, "power-sim-par-{}-input.json".format(node))
            power_par_in = os.path.join(obj_dir, "power-par-{}-input.json".format(node))
            power_par_out = os.path.join(power_par_run_dir, "power-output-full.json")
            drc_in = os.path.join(obj_dir, "drc-{}-input.json".format(node))
            drc_out = os.path.join(drc_run_dir, "drc-output-full.json")
            lvs_in = os.path.join(obj_dir, "lvs-{}-input.json".format(node))
            lvs_out = os.path.join(lvs_run_dir, "lvs-output-full.json")
            formal_syn_in = os.path.join(obj_dir, "formal-syn-{}-input.json".format(node))
            formal_syn_out = os.path.join(formal_syn_run_dir, "formal-output-full.json")
            formal_par_in = os.path.join(obj_dir, "formal-par-{}-input.json".format(node))
            formal_par_out = os.path.join(formal_par_run_dir, "formal-output-full.json")
            timing_syn_in = os.path.join(obj_dir, "timing-syn-{}-input.json".format(node))
            timing_syn_out = os.path.join(timing_syn_run_dir, "timing-output-full.json")
            timing_par_in = os.path.join(obj_dir, "timing-par-{}-input.json".format(node))
            timing_par_out = os.path.join(timing_par_run_dir, "timing-output-full.json")

            # need to revert this each time
            syn_deps = "$(HAMMER_DEPENDENCIES)"
            par_to_syn = ""
            if len(out_edges) > 0:
                syn_deps = os.path.join(obj_dir, "syn-{}-input.json".format(node))
                p_syn_in = "-p {}".format(syn_deps)
                out_confs = [os.path.join(obj_dir, "par-" + x, "par-output-full.json") for x in out_edges]
                prereqs = " ".join(out_confs)
                pstring = " ".join(["-p " + x for x in out_confs])
                par_to_syn = textwrap.dedent("""
                    .PHONY: hier-par-to-syn-{node} redo-hier-par-to-syn-{node}

                    {syn_deps}: {prereqs}
                    \t$(HAMMER_EXEC) {env_confs} {pstring} -o {syn_deps} --obj_dir {obj_dir} hier-par-to-syn

                    hier-par-to-syn-{node}: {syn_deps}

                    redo-hier-par-to-syn-{node}:
                    \t$(HAMMER_EXEC) {env_confs} {pstring} -o {syn_deps} --obj_dir {obj_dir} hier-par-to-syn
                    """.format(syn_deps=syn_deps, prereqs=prereqs, env_confs=env_confs, pstring=pstring,
                    obj_dir=obj_dir, node=node))

            output += make_text.format(suffix="-"+node, mod=node, env_confs=env_confs, obj_dir=obj_dir, syn_deps=syn_deps,
                par_to_syn=par_to_syn,
                p_sim_rtl_in=p_sim_rtl_in, sim_rtl_out=sim_rtl_out, sim_rtl_run_dir=sim_rtl_run_dir,
                power_sim_rtl_in=power_sim_rtl_in, power_rtl_out=power_rtl_out, power_rtl_run_dir=power_rtl_run_dir,
                sim_syn_in=sim_syn_in, sim_syn_out=sim_syn_out, sim_syn_run_dir=sim_syn_run_dir,
                sim_par_in=sim_par_in, sim_par_out=sim_par_out, sim_par_run_dir=sim_par_run_dir,
                p_syn_in=p_syn_in, syn_out=syn_out, par_in=par_in, par_out=par_out,
                power_sim_syn_in=power_sim_syn_in, power_syn_in=power_syn_in, power_syn_out=power_syn_out, power_syn_run_dir=power_syn_run_dir,
                power_sim_par_in=power_sim_par_in, power_par_in=power_par_in, power_par_out=power_par_out, power_par_run_dir=power_par_run_dir,
                drc_in=drc_in, drc_out=drc_out, lvs_in=lvs_in, lvs_out=lvs_out,
                formal_syn_in=formal_syn_in, formal_syn_out=formal_syn_out, formal_syn_run_dir=formal_syn_run_dir,
                formal_par_in=formal_par_in, formal_par_out=formal_par_out, formal_par_run_dir=formal_par_run_dir,
                timing_syn_in=timing_syn_in, timing_syn_out=timing_syn_out, timing_syn_run_dir=timing_syn_run_dir,
                timing_par_in=timing_par_in, timing_par_out=timing_par_out, timing_par_run_dir=timing_par_run_dir)

    with open(makefile, "w") as f:
        f.write(output)

    return dependency_graph


# ---------------------------------------------------------------------------
# Airflow DAG generator (from Andre's dag_gen branch, repackaged as the
# 'airflow' build system so 'make' keeps emitting hammer.d).
# ---------------------------------------------------------------------------

def build_airflow_dag(driver: HammerDriver, append_error_func: Callable[[str], None],
                      proj_confs_by_tools=None, default_tools=None, hammer_exec: Optional[str] = None) -> dict:
    # Emit hammer.d as well. Choosing the DAG should add a way to run the flow,
    # not take one away: projects document `make syn`, `make par-<block>` and
    # the redo- targets, and those come from the makefile include. Both
    # generators write their own file into obj_dir, so keeping the legacy one
    # costs nothing and leaves every existing command working.
    # hammer_exec: the Hammer driver every task runs. It defaults to the executable running
    # this build, which is right for `hammer-vlsi build` / `make buildfile`; a caller that is
    # not itself a Hammer driver (studio make-dag) must pass one.
    build_makefile(driver, append_error_func, hammer_exec=hammer_exec)

    dependency_graph = driver.get_hierarchical_dependency_graph()
    dag_file = os.path.join(driver.obj_dir, "hammer_dag.py")
    
    # Extract top module name dynamically to cleanly uniqueness check DAG namespaces
    top_module = str(driver.database.get_setting("synthesis.inputs.top_module"))
    
    env_confs = [os.path.realpath(x) for x in driver.options.environment_configs]
    proj_confs = [os.path.realpath(x) for x in driver.options.project_configs]
    obj_dir = os.path.realpath(driver.obj_dir)
    hammer_exec = os.path.realpath(hammer_exec or sys.argv[0])
    # Pin the exact interpreter that generated this DAG (the venv python).
    # The generated DAG invokes hammer as `[HAMMER_PY, HAMMER_EXEC, ...]` so
    # task subprocesses don't depend on the venv being on PATH -- Airflow
    # workers (and `airflow dags test`) often run with a sanitized PATH where
    # `#!/usr/bin/env python3` would otherwise miss the editable hammer install.
    # NOTE: do NOT realpath() this -- the venv python is a symlink to the base
    # interpreter, and resolving it drops the venv's site-packages (where the
    # editable hammer install lives). sys.executable keeps the venv path.
    hammer_py = sys.executable
    # Directory the build was invoked from. Designs often use RTL/config paths
    # relative to this dir (e.g. input_files: ["src/pass.v"] resolves against
    # CWD), so each task subprocess must run from here -- Airflow workers have
    # their own CWD and would otherwise miss those relative paths.
    work_dir = os.getcwd()

    env_str = ", ".join([f"'{x}'" for x in env_confs])
    proj_str = ", ".join([f"'{x}'" for x in proj_confs])

    # Runtime tools dropdown. make-dag passes {tool: [configs]} (auto-discovered
    # from configs-tool/*.yml) so the generated DAG offers each tool as a
    # trigger-time choice; the plain `hammer-vlsi build` path passes nothing, so
    # fall back to a single 'default' entry from the driver's project_configs.
    if proj_confs_by_tools:
        tools_map = {t: [os.path.realpath(p) for p in confs]
                     for t, confs in proj_confs_by_tools.items()}
        default_tool = (default_tools if default_tools in tools_map
                        else sorted(tools_map)[0])
    else:
        tools_map = {"default": proj_confs}
        default_tool = "default"
    # Python dict literal for the template (repr keeps path quoting correct).
    proj_by_tools_str = "{" + ", ".join(
        f"{t!r}: [{', '.join(repr(p) for p in confs)}]"
        for t, confs in tools_map.items()
    ) + "}"

    # design_name = obj_dir basename (build-<pdk>-<tools>/<design>). The per-user
    # workspace resolver uses it to pick <workspace_root>/<design> at run time.
    design_name = os.path.basename(obj_dir.rstrip("/")) or top_module

    # DAG id must be globally unique once DAGs share one Airflow instance (the
    # SledgeHammer Studio shared-workspace goal; Airflow keeps only ONE DAG per
    # dag_id). top_module ALONE is not unique: many Chipyard configs share a
    # top_module (e.g. ChipTop), and two users building the SAME design would
    # collide. Default format is `sledgehammer_<module>_<user>` (tool_module_user,
    # where module = design_name); an explicit override via `make-dag --dag-id`
    # (vlsi.core.airflow_dag_id) wins if set. Sanitize to Airflow's allowed
    # dag_id charset [A-Za-z0-9_.-].
    try:
        gen_user = os.environ.get("USER") or getpass.getuser()
    except Exception:
        gen_user = "unknown"
    from hammer.vlsi import sledge_settings
    dag_id_override = sledge_settings.text(driver, "vlsi.core.airflow_dag_id")
    unique_dag_id = re.sub(
        r"[^A-Za-z0-9_.-]", "_",
        str(dag_id_override) if dag_id_override
        else f"sledgehammer_{design_name}_{gen_user}",
    )

    # Per-user edge execution: vlsi.core.airflow_edge routes every task of this
    # DAG to an EdgeExecutor queue (default: the generating user's name, or
    # vlsi.core.airflow_queue). The owner runs `studio-worker.sh` (or
    # `airflow edge worker --queues <name>`) as themselves; the shared
    # deployment then only schedules -- it never executes this DAG's tasks.
    edge_on = sledge_settings.flag(driver, "vlsi.core.airflow_edge", False)
    edge_queue = None
    if edge_on:
        edge_queue = sledge_settings.text(driver, "vlsi.core.airflow_queue") or gen_user
        edge_queue = re.sub(r"[^A-Za-z0-9_.-]", "_", edge_queue)

    # 1. Base DAG Header & Safe Execution Subprocess Wrapper
    output = textwrap.dedent(f"""\
        # Auto-generated Airflow DAG by Hammer Build System
        import os
        import sys
        import pendulum
        import subprocess
        from datetime import datetime, timedelta
        from airflow.decorators import task, dag
        from airflow.models import Param
        from airflow.utils.task_group import TaskGroup
        from airflow.utils.trigger_rule import TriggerRule

        # Flow-completion emails. Resolved at parse time; a missing/undeliverable
        # notifier must never break DAG parsing, so failure degrades to None.
        try:
            from hammer.vlsi.pd_notify import notify_flow_complete as _notify_flow_complete
        except Exception:
            _notify_flow_complete = None
        from airflow.exceptions import AirflowSkipException, AirflowFailException

        HAMMER_EXEC = "{hammer_exec}"
        HAMMER_PY = "{hammer_py}"
        WORK_DIR = "{work_dir}"
        OBJ_DIR = "{obj_dir}"
        ENV_CONFIGS = [{env_str}]
        PROJ_CONFIGS = [{proj_str}]

        DESIGN_NAME = "{design_name}"
        GEN_USER = "{gen_user}"
        # Tools dropdown {{tool: [configs]}}: make-dag fills this from
        # configs-tool/*.yml so each tool is selectable at trigger time via the
        # 'tools' Param; the plain build path emits a single 'default' entry.
        PROJ_CONFS_BY_TOOLS = {proj_by_tools_str}
        DEFAULT_TOOLS = "{default_tool}"
        TOOLS_CHOICES = sorted(PROJ_CONFS_BY_TOOLS.keys())

        def _selected_proj_configs(context):
            \"\"\"Project configs for the trigger's 'tools' choice: dag_run.conf, then params, then DEFAULT_TOOLS.\"\"\"
            conf = context['dag_run'].conf or {{}}
            choice = conf.get('tools') or (context.get('params') or {{}}).get('tools') or DEFAULT_TOOLS
            if choice not in PROJ_CONFS_BY_TOOLS:
                raise AirflowFailException(f"Unknown tools {{choice!r}}; choose one of {{TOOLS_CHOICES}}")
            return PROJ_CONFS_BY_TOOLS[choice]

        # Per-user edge execution (vlsi.core.airflow_edge). When a queue is set,
        # every task in this DAG is routed to the EdgeExecutor on that queue, so
        # the DAG owner's own edge worker -- running as THEIR Unix user -- does
        # the work instead of the shared deployment's user. None -> unchanged
        # local execution.
        EDGE_QUEUE = {edge_queue!r}
        EDGE_EXECUTOR = "airflow.providers.edge3.executors.EdgeExecutor"
        if EDGE_QUEUE:
            _base_task = task

            def task(fn=None, **kw):
                kw.setdefault("queue", EDGE_QUEUE)
                kw.setdefault("executor", EDGE_EXECUTOR)
                if fn is not None:
                    return _base_task(**kw)(fn)
                return _base_task(**kw)

        default_args = {{
            'owner': 'hammer',
            'start_date': pendulum.datetime(2026, 1, 1, tz="UTC"),
            'retries': 3,
            'retry_delay': timedelta(seconds=5),
        }}

        def run_hammer_action(action, extra_flags=None):
            \"\"\"
            Spawns a clean python subprocess executing the target stage.

            The one chokepoint every stage calls:
              * per-user workspace: resolve the triggering user's OBJ_DIR at run
                time so two users never share a build dir
              * cache provenance: the resolver also pins HAMMER_AIRFLOW_* env that
                pd_cache stamps onto each stored blob
            \"\"\"
            action_clean = str(action).strip()
            if action_clean.endswith("None"):
                action_clean = action_clean[:-4]

            # Pull the live task context without threading it through every task.
            context = None
            try:
                from airflow.sdk import get_current_context
                context = get_current_context()
            except Exception:
                try:
                    from airflow.operators.python import get_current_context
                    context = get_current_context()
                except Exception:
                    context = None

            # Per-user workspace + provenance. _resolve_workspace_obj_dir returns the
            # triggering user's <workspace_root>/<design> and sets HAMMER_AIRFLOW_*
            # env for the cache layer. Falls back to the gen-time OBJ_DIR otherwise.
            obj_dir = OBJ_DIR
            if context is not None:
                try:
                    from hammer.shell.hammer_vlsi import _resolve_workspace_obj_dir
                    resolved = _resolve_workspace_obj_dir(
                        context, DESIGN_NAME,
                        default_obj_dir=OBJ_DIR, gen_user=GEN_USER)
                    if resolved:
                        obj_dir = resolved
                except Exception as e:
                    # A lock conflict means another live run owns the build dir.
                    # Falling back to the baked OBJ_DIR here would walk straight
                    # into that directory and demolish the other run's work, so
                    # only resolver-infrastructure trouble may fall through.
                    # The same goes for a non-owner with no registered
                    # workspace: the baked OBJ_DIR is the owner's.
                    if type(e).__name__ in ("RunLockConflict", "WorkspaceNotRegistered"):
                        raise
                    print(f"[workspace] resolver unavailable ({{e}}); using gen-time OBJ_DIR")

            # Redirect any gen-time OBJ_DIR-prefixed paths to the per-user workspace.
            if obj_dir != OBJ_DIR and extra_flags:
                extra_flags = [f.replace(OBJ_DIR, obj_dir) if isinstance(f, str) else f
                               for f in extra_flags]

            print(f"Running active Hammer Action: {{action_clean}}")

            cmd = [HAMMER_PY, HAMMER_EXEC]
            for env in ENV_CONFIGS:
                cmd += ["-e", env]

            if extra_flags and str(extra_flags) != "None":
                cmd += extra_flags

            # Per-run modifiers. 'local' adds --local to every stage; 'redo' adds
            # --force only to the selected stages, 'forceall' to every stage this
            # run executes. Bridge actions never take --force.
            _mods = {{}}
            if context is not None:
                try:
                    _mods = context['dag_run'].conf or {{}}
                except Exception:
                    _mods = {{}}
            if _mods.get('local'):
                cmd += ["--local"]
            _force = bool(_mods.get('forceall') or _mods.get('redo'))
            if _force and context is not None:
                try:
                    _task = context['task']
                    _force = _force_requested(_mods, _task.task_id,
                                              _module_selected(_task_module(_task), _mods))
                except Exception as e:
                    print(f"[redo] per-stage force check failed ({{e}}); forcing this stage")
            if _force and "-to-" not in action_clean and not action_clean.startswith("hier-"):
                cmd += ["--force"]

            # Sub-step flow control from the trigger form. The flags attach only
            # to the chosen stage's tasks (default syn) and never to bridge
            # actions, so a from_step meant for syn can't break par or syn-to-par.
            _steps_stage = str(_mods.get('steps_stage') or 'syn')
            _is_stage_task = ("-to-" not in action_clean
                              and not action_clean.startswith("hier-")
                              and (action_clean == _steps_stage
                                   or action_clean.startswith(_steps_stage + "-")))
            if _is_stage_task:
                for _flag in ("from_step", "to_step", "only_step"):
                    _val = str(_mods.get(_flag) or "").strip()
                    if _val:
                        cmd += ["--" + _flag, _val]

            cmd += ["--obj_dir", obj_dir, action_clean]

            print(f"Executing Process Command: {{' '.join(cmd)}}")
            # hammer imports `airflow` on startup (cli_driver), which initializes
            # the Airflow ORM and parses AIRFLOW__DATABASE__SQL_ALCHEMY_CONN.
            # Airflow injects a sanitized/unparseable value into task subprocesses
            # (tasks aren't allowed the metadata DB), so strip it and let hammer's
            # airflow import fall back to airflow.cfg's real conn.
            sub_env = dict(os.environ)
            for _k in ("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN",
                       "AIRFLOW__CORE__SQL_ALCHEMY_CONN"):
                sub_env.pop(_k, None)
            # A malloc arena cap the launcher set is for Airflow's own processes, not hammer or the EDA tools.
            if sub_env.pop("SLEDGE_SET_MALLOC_ARENA_MAX", None):
                sub_env.pop("MALLOC_ARENA_MAX", None)
            # stdin closed so a tool that errors out and falls back to an
            # interactive prompt (innovus does this) exits on EOF instead of
            # hanging the task forever.
            import time
            _t0 = time.time()
            res = subprocess.run(cmd, cwd=WORK_DIR, env=sub_env,
                                 stdin=subprocess.DEVNULL)
            _state = "success" if res.returncode == 0 else "failed"
            # stage outcomes feed the completion email; bridge actions are
            # bookkeeping rather than stages, so they stay out of it
            if context is not None and "-to-" not in action_clean and not action_clean.startswith("hier-"):
                _secs = time.time() - _t0
                try:
                    from hammer.vlsi.pd_notify import record_stage_outcome
                    record_stage_outcome(context["dag_run"].dag_id,
                                         context["dag_run"].run_id,
                                         context["ti"].task_id,
                                         action_clean, _state, _secs)
                except Exception as _e:
                    print(f"[notify] stage outcome not recorded: {{_e}}")
                # mail per stage: failures right away, successes when the stage
                # was long enough to be worth a note. stage_mails=false in the
                # trigger conf silences these; the end-of-run summary still goes.
                if _mods.get("stage_mails", True) and (_state == "failed" or _secs >= 120):
                    try:
                        from hammer.vlsi.pd_notify import notify_stage_finished
                        notify_stage_finished(context, context["ti"].task_id,
                                              action_clean, _state, _secs,
                                              gen_user=GEN_USER)
                    except Exception as _e:
                        print(f"[notify] stage mail not sent: {{_e}}")
            if res.returncode != 0:
                raise AirflowFailException(f"Hammer action {{action_clean}} failed with exit code {{res.returncode}}")

        def _task_module(task_obj):
            \"\"\"Module name from a task's group id (module_<Name>), or None for top-level tasks.\"\"\"
            tid = task_obj.task_id
            if "." in tid:
                group = tid.rsplit(".", 1)[0]
                if group.startswith("module_"):
                    return group[len("module_"):]
            return None

        def _module_selected(module, conf):
            \"\"\"True if this module is in the run's 'modules' selection ('all' by default).

            Hierarchical flows: pick modules from the trigger form's dropdown (or
            pass a list / comma-separated string in conf) to run stages on just
            those modules instead of every module in the tree. Top-level tasks
            (start, exit_, bridges) have no module and always pass.
            \"\"\"
            sel = conf.get('modules') or ['all']
            if isinstance(sel, str):
                sel = sel.split(',')
            sel = [str(m).strip() for m in sel if str(m).strip()]
            if not sel or any(m.lower() == 'all' for m in sel) or module is None:
                return True
            return module in sel

        def _force_requested(conf, task_id, module_selected):
            \"\"\"Whether a stage task gets --force: 'forceall' forces every stage, 'redo' only the selected ones.\"\"\"
            if conf.get('forceall'):
                return True
            return bool(conf.get('redo')) and bool(conf.get(task_id.split('.')[-1])) and module_selected

        def should_run_stage(stage_key, context):
            \"\"\"
            Determines if a stage should execute based on whether it was explicitly
            selected OR if any downstream dependent tasks are active. Both checks
            are scoped by the 'modules' selection, so a stage in a deselected
            module neither runs nor pulls its upstream stages in.
            \"\"\"
            conf = context['dag_run'].conf
            if conf.get(stage_key, False) and _module_selected(_task_module(context['task']), conf):
                return True

            def check_downstream_active(task_obj):
                for downstream in task_obj.downstream_list:
                    downstream_id = downstream.task_id.split('.')[-1]
                    if conf.get(downstream_id, False) and _module_selected(_task_module(downstream), conf):
                        return True
                    if check_downstream_active(downstream):
                        return True
                return False

            return check_downstream_active(context['task'])
    """)

    # 2. Dynamic Python Task Generator Logic
    output += textwrap.dedent("""
        @task
        def sim_rtl(suffix, p_sim_rtl_in, sim_rtl_run_dir, **context):
            if should_run_stage('sim_rtl', context) or should_run_stage('power_rtl', context):
                if p_sim_rtl_in == PROJ_CONFIGS:
                    p_sim_rtl_in = _selected_proj_configs(context)
                flags = []
                for p in p_sim_rtl_in:
                    flags += ["-p", p]
                flags += ["--sim_rundir", sim_rtl_run_dir]
                run_hammer_action(f"sim{suffix}", flags)
            else:
                raise AirflowSkipException("sim_rtl task skipped")

        @task
        def sim_to_power(sim_rtl_out, power_sim_rtl_in, **context):
            if should_run_stage('power_rtl', context) or should_run_stage('power_syn', context) or should_run_stage('power_par', context):
                run_hammer_action("sim-to-power", ["-p", sim_rtl_out, "-o", power_sim_rtl_in])
            else:
                raise AirflowSkipException("sim-to-power skipped")

        @task
        def power_rtl(suffix, power_sim_rtl_in, power_rtl_run_dir, **context):
            if should_run_stage('power_rtl', context):
                run_hammer_action(f"power{suffix}", ["-p", power_sim_rtl_in, "--power_rundir", power_rtl_run_dir])
            else:
                raise AirflowSkipException("power_rtl task skipped")

        @task
        def syn(suffix, p_syn_in, **context):
            if should_run_stage('syn', context):
                if p_syn_in == PROJ_CONFIGS:
                    p_syn_in = _selected_proj_configs(context)
                flags = []
                for p in p_syn_in:
                    flags += ["-p", p]
                run_hammer_action(f"syn{suffix}", flags)
            else:
                raise AirflowSkipException("syn task skipped")

        @task
        def syn_to_sim(syn_out, sim_syn_in, **context):
            if should_run_stage('sim_syn', context) or should_run_stage('power_syn', context):
                run_hammer_action("syn-to-sim", ["-p", syn_out, "-o", sim_syn_in])
            else:
                raise AirflowSkipException("syn-to-sim skipped")

        @task
        def sim_syn(suffix, sim_syn_in, sim_syn_run_dir, **context):
            if should_run_stage('sim_syn', context) or should_run_stage('power_syn', context):
                run_hammer_action(f"sim{suffix}", ["-p", sim_syn_in, "--sim_rundir", sim_syn_run_dir])
            else:
                raise AirflowSkipException("sim_syn task skipped")

        @task
        def syn_to_power(syn_out, power_syn_in, **context):
            if should_run_stage('power_syn', context):
                run_hammer_action("syn-to-power", ["-p", syn_out, "-o", power_syn_in])
            else:
                raise AirflowSkipException("syn-to-power skipped")

        @task
        def power_syn(suffix, power_sim_syn_in, power_syn_in, power_syn_run_dir, **context):
            if should_run_stage('power_syn', context):
                run_hammer_action(f"power{suffix}", ["-p", power_sim_syn_in, "-p", power_syn_in, "--power_rundir", power_syn_run_dir])
            else:
                raise AirflowSkipException("power_syn task skipped")

        @task
        def syn_to_par(syn_out, par_in, **context):
            if should_run_stage('par', context) or should_run_stage('drc', context) or should_run_stage('lvs', context) or should_run_stage('sim_par', context) or should_run_stage('timing_par', context) or should_run_stage('formal_par', context) or should_run_stage('power_par', context):
                run_hammer_action("syn-to-par", ["-p", syn_out, "-o", par_in])
            else:
                raise AirflowSkipException("syn-to-par skipped")

        @task
        def par(suffix, par_in, **context):
            if should_run_stage('par', context):
                run_hammer_action(f"par{suffix}", ["-p", par_in])
            else:
                raise AirflowSkipException("par task skipped")

        @task
        def par_to_sim(par_out, sim_par_in, **context):
            if should_run_stage('sim_par', context) or should_run_stage('power_par', context):
                run_hammer_action("par-to-sim", ["-p", par_out, "-o", sim_par_in])
            else:
                raise AirflowSkipException("par-to-sim skipped")

        @task
        def sim_par(suffix, sim_par_in, sim_par_run_dir, **context):
            if should_run_stage('sim_par', context) or should_run_stage('power_par', context):
                run_hammer_action(f"sim{suffix}", ["-p", sim_par_in, "--sim_rundir", sim_par_run_dir])
            else:
                raise AirflowSkipException("sim_par task skipped")

        @task
        def par_to_power(par_out, power_par_in, **context):
            if should_run_stage('power_par', context):
                run_hammer_action("par-to-power", ["-p", par_out, "-o", power_par_in])
            else:
                raise AirflowSkipException("par-to-power skipped")

        @task
        def power_par(suffix, power_sim_par_in, power_par_in, power_par_run_dir, **context):
            if should_run_stage('power_par', context):
                run_hammer_action(f"power{suffix}", ["-p", power_sim_par_in, "-p", power_par_in, "--power_rundir", power_par_run_dir])
            else:
                raise AirflowSkipException("power_par task skipped")

        @task
        def par_to_formal(par_out, formal_par_in, **context):
            if should_run_stage('formal_par', context):
                run_hammer_action("par-to-formal", ["-p", par_out, "-o", formal_par_in])
            else:
                raise AirflowSkipException("par-to-formal skipped")

        @task
        def formal_par(suffix, formal_par_in, formal_par_run_dir, **context):
            if should_run_stage('formal_par', context):
                run_hammer_action(f"formal{suffix}", ["-p", formal_par_in, "--formal_rundir", formal_par_run_dir])
            else:
                raise AirflowSkipException("formal_par task skipped")

        @task
        def par_to_timing(par_out, timing_par_in, **context):
            if should_run_stage('timing_par', context):
                run_hammer_action("par-to-timing", ["-p", par_out, "-o", timing_par_in])
            else:
                raise AirflowSkipException("par-to-timing skipped")

        @task
        def timing_par(suffix, timing_par_in, timing_par_run_dir, **context):
            if should_run_stage('timing_par', context):
                run_hammer_action(f"timing{suffix}", ["-p", timing_par_in, "--timing_rundir", timing_par_run_dir])
            else:
                raise AirflowSkipException("timing_par task skipped")

        @task
        def par_to_drc(par_out, drc_in, **context):
            if should_run_stage('drc', context):
                run_hammer_action("par-to-drc", ["-p", par_out, "-o", drc_in])
            else:
                raise AirflowSkipException("par-to-drc skipped")

        @task
        def drc(suffix, drc_in, **context):
            if should_run_stage('drc', context):
                run_hammer_action(f"drc{suffix}", ["-p", drc_in])
            else:
                raise AirflowSkipException("drc task skipped")

        @task
        def par_to_lvs(par_out, lvs_in, **context):
            if should_run_stage('lvs', context):
                run_hammer_action("par-to-lvs", ["-p", par_out, "-o", lvs_in])
            else:
                raise AirflowSkipException("par-to-lvs skipped")

        @task
        def lvs(suffix, lvs_in, **context):
            if should_run_stage('lvs', context):
                run_hammer_action(f"lvs{suffix}", ["-p", lvs_in])
            else:
                raise AirflowSkipException("lvs task skipped")

        @task
        def syn_to_formal(syn_out, formal_syn_in, **context):
            if should_run_stage('formal_syn', context):
                run_hammer_action("syn-to-formal", ["-p", syn_out, "-o", formal_syn_in])
            else:
                raise AirflowSkipException("syn-to-formal skipped")

        @task
        def formal_syn(suffix, formal_syn_in, formal_syn_run_dir, **context):
            if should_run_stage('formal_syn', context):
                run_hammer_action(f"formal{suffix}", ["-p", formal_syn_in, "--formal_rundir", formal_syn_run_dir])
            else:
                raise AirflowSkipException("formal_syn task skipped")

        @task
        def syn_to_timing(syn_out, timing_syn_in, **context):
            if should_run_stage('timing_syn', context):
                run_hammer_action("syn-to-timing", ["-p", syn_out, "-o", timing_syn_in])
            else:
                raise AirflowSkipException("syn-to-timing skipped")

        @task
        def timing_syn(suffix, timing_syn_in, timing_syn_run_dir, **context):
            if should_run_stage('timing_syn', context):
                run_hammer_action(f"timing{suffix}", ["-p", timing_syn_in, "--timing_rundir", timing_syn_run_dir])
            else:
                raise AirflowSkipException("timing_syn task skipped")

        @task(task_id="hier_par_to_syn")
        def hier_par_to_syn(pstring, syn_deps, **context):
            flags = []
            for ps in pstring:
                flags += ["-p", ps]
            flags += ["-o", syn_deps]
            run_hammer_action("hier-par-to-syn", flags)
    """)

    # 3. Parameter Inputs & Unique DAG Skeleton Generation
    # Module choices for the trigger-time 'modules' dropdown: hierarchy members
    # for a hierarchical flow, just the top module for a flat one.
    module_examples = ["all"] + (list(dependency_graph.keys()) if dependency_graph else [top_module])
    _syn_steps = "init_environment, syn_generic, syn_map, add_tieoffs, write_regs, generate_reports, write_outputs"
    _par_steps = ("floorplan_design, place_bumps, place_tap_cells, power_straps, place_pins, "
                  "place_opt_design, clock_tree, add_fillers, route_opt_design, write_regs, write_design")
    _steps_hint = (f"Sub-steps -- syn: {_syn_steps}.\\n\\npar: {_par_steps}.\\n\\n"
                   "Custom hook steps (e.g. techname-prefixed) also appear; the authoritative live "
                   "list is the pre_<step> dirs in the rundir or `studio checkpoints`.")
    output += f"""
@dag(
    dag_id='{unique_dag_id}',
    default_args=default_args,
    schedule=None,
    catchup=False,
    on_success_callback=_notify_flow_complete,
    on_failure_callback=_notify_flow_complete,
    params={{
        'sim_rtl': Param(default=False, type='boolean', title='RTL Simulation'),
        'power_rtl': Param(default=False, type='boolean', title='RTL Power Simulation'),
        'syn': Param(default=False, type='boolean', title='Synthesis'),
        'sim_syn': Param(default=False, type='boolean', title='Simulation Synthesis'),
        'timing_syn': Param(default=False, type='boolean', title='Timing Synthesis'),
        'formal_syn': Param(default=False, type='boolean', title='Formal Synthesis'),
        'power_syn': Param(default=False, type='boolean', title='Power Synthesis'),
        'par': Param(default=False, type='boolean', title='Place and Route'),
        'drc': Param(default=False, type='boolean', title='Design Rule Check'),
        'lvs': Param(default=False, type='boolean', title='Layout Versus Schematic'),
        'sim_par': Param(default=False, type='boolean', title='Simulation Place and Route'),
        'timing_par': Param(default=False, type='boolean', title='Timing Place and Route'),
        'formal_par': Param(default=False, type='boolean', title='Formal Place and Route'),
        'power_par': Param(default=False, type='boolean', title='Power Place and Route'),
        'modules': Param(default=['all'], type='array', examples={module_examples!r}, title='Modules',
                         description='Run the selected stages on these modules only (default: all).'),
        'tools': Param(default=DEFAULT_TOOLS, type='string', enum=TOOLS_CHOICES, title='Tools config'),
        'local': Param(default=False, type='boolean', title='Local (skip DB cache pull; run the tool locally)'),
        'redo': Param(default=False, type='boolean', title='Redo (force the selected stages)'),
        'forceall': Param(default=False, type='boolean', title='Redo all (force every stage this run executes)'),
        'from_step': Param(default='', type=['string', 'null'], title='From step',
                           description_md='Start the selected stage from this sub-step, loading its pre_<step> checkpoint from the rundir. Leave empty for automatic resume.\\n\\n{_steps_hint}'),
        'to_step': Param(default='', type=['string', 'null'], title='To step',
                         description_md='Stop the selected stage after this sub-step (its checkpoint stays resumable).\\n\\n{_steps_hint}'),
        'only_step': Param(default='', type=['string', 'null'], title='Only step',
                           description_md='Run exactly this one sub-step of the selected stage.\\n\\n{_steps_hint}'),
        'steps_stage': Param(default='syn', type='string', enum=['syn', 'par'], title='Step flags apply to',
                             description='Which stage the from/to/only step fields target.'),
    }},
    render_template_as_native_obj=True
)
def hammer_dag():

    @task(task_id="start")
    def start(**context):
        print("Starting Hammer Flow Pipeline Execution Orchestration...")

    # ALL_DONE so the savings summary prints even when an upstream stage
    # fails; a run that cached syn but died in par still reports the time it
    # saved (and its event log still clears). exit_ stays NONE_FAILED so a
    # failed stage keeps failing the run -- if this task were the terminal
    # ALL_DONE leaf instead, every failed run would show up green.
    @task(task_id="cache_summary_", trigger_rule=TriggerRule.ALL_DONE)
    def cache_summary_(**context):
        # Print the per-run PD cache savings summary
        # (wall + CPU time saved) and clear the run's event log.
        run_id = None
        try:
            run_id = context["dag_run"].run_id
        except Exception:
            run_id = None
        # ALL_DONE, so this is the one task that runs however the flow ended.
        # Hand the obj_dir back before anything else, or a failed run keeps it.
        # Tasks are separate processes, so resolve the directory the same way
        # the stages did rather than reading OBJ_DIR out of this one's env.
        try:
            from hammer.shell.hammer_vlsi import _resolve_workspace_obj_dir, release_obj_dir
            obj_dir = OBJ_DIR
            try:
                resolved = _resolve_workspace_obj_dir(
                    context, DESIGN_NAME, default_obj_dir=OBJ_DIR, gen_user=GEN_USER,
                    claim=False)
                if resolved:
                    obj_dir = resolved
            except Exception:
                pass
            release_obj_dir(obj_dir, run_id)
        except Exception as e:
            print(f"WARNING: could not release the run lock: {{e}}")
        if run_id:
            try:
                from hammer.vlsi.time_tracking import read_run_cache_summary, clear_run_cache_events
                summary = read_run_cache_summary(run_id)
                if summary:
                    print("=" * 72)
                    print(f"PD CACHE SUMMARY for run {{run_id}}")
                    print("=" * 72)
                    print(summary)
                    print("=" * 72)
                    clear_run_cache_events(run_id)
            except Exception as e:
                print(f"[cache-summary] skipped: {{e}}")

    @task(task_id="exit_", trigger_rule=TriggerRule.NONE_FAILED)
    def exit_(**context):
        print("Exiting flow safely.")
        return "ok"

    # The completion mail is sent from a task, not from the DAG's
    # on_success/on_failure callbacks: Airflow 3.1 serializes a DAG without any
    # record of those callbacks, so the scheduler never dispatches them and
    # they go silently unrun. ALL_DONE means this runs however the flow ended.
    # exit_ only succeeds when nothing failed, so its XCom is the outcome
    # signal. We re-raise on a bad flow to keep a failed run red -- this is the
    # terminal leaf now, and a leaf that always succeeded would paint every
    # failed run green.
    @task(task_id="notify_", trigger_rule=TriggerRule.ALL_DONE, retries=0)
    def notify_(**context):
        ok = False
        try:
            ti = context.get("ti")
            ok = ti is not None and ti.xcom_pull(task_ids="exit_") == "ok"
        except Exception as e:
            print(f"[notify] could not read the exit_ status: {{e}}")
        state = "success" if ok else "failed"
        try:
            from hammer.vlsi.pd_notify import notify_run_finished
            notify_run_finished(context, state, gen_user=GEN_USER)
        except Exception as e:
            print(f"[notify] completion mail skipped: {{e}}")
        if not ok:
            raise AirflowFailException("flow failed upstream; completion mail sent")

    def create_module_pipeline(mod_name, suffix, paths_dict):
        with TaskGroup(group_id=f"module_{{mod_name or 'Top'}}") as tg:
            
            # 1. RTL Simulation Track
            s_rtl = sim_rtl(suffix, paths_dict['p_sim_rtl_in'], paths_dict['sim_rtl_run_dir'])
            s_rtl_to_p = sim_to_power(paths_dict['sim_rtl_out'], paths_dict['power_sim_rtl_in'])
            p_rtl = power_rtl(suffix, paths_dict['power_sim_rtl_in'], paths_dict['power_rtl_run_dir'])

            # 2. Synthesis Track
            s_node = syn(suffix, paths_dict['p_syn_in'])
            s_to_sim = syn_to_sim(paths_dict['syn_out'], paths_dict['sim_syn_in'])
            s_syn = sim_syn(suffix, paths_dict['sim_syn_in'], paths_dict['sim_syn_run_dir'])
            s_to_p = syn_to_power(paths_dict['syn_out'], paths_dict['power_syn_in'])
            s_syn_to_p = sim_to_power(paths_dict['sim_syn_out'], paths_dict['power_sim_syn_in'])
            p_syn = power_syn(suffix, paths_dict['power_sim_syn_in'], paths_dict['power_syn_in'], paths_dict['power_syn_run_dir'])

            # Post-Synthesis Verification Steps
            s_to_form = syn_to_formal(paths_dict['syn_out'], paths_dict['formal_syn_in'])
            f_syn = formal_syn(suffix, paths_dict['formal_syn_in'], paths_dict['formal_syn_run_dir'])
            s_to_time = syn_to_timing(paths_dict['syn_out'], paths_dict['timing_syn_in'])
            t_syn = timing_syn(suffix, paths_dict['timing_syn_in'], paths_dict['timing_syn_run_dir'])

            # 3. Place & Route Track
            s_to_par = syn_to_par(paths_dict['syn_out'], paths_dict['par_in'])
            p_node = par(suffix, paths_dict['par_in'])

            # Post-P&R Verification Signoffs
            p_to_sim = par_to_sim(paths_dict['par_out'], paths_dict['sim_par_in'])
            s_par = sim_par(suffix, paths_dict['sim_par_in'], paths_dict['sim_par_run_dir'])
            p_to_p = par_to_power(paths_dict['par_out'], paths_dict['power_par_in'])
            s_par_to_p = sim_to_power(paths_dict['sim_par_out'], paths_dict['power_sim_par_in'])
            p_par = power_par(suffix, paths_dict['power_sim_par_in'], paths_dict['power_par_in'], paths_dict['power_par_run_dir'])

            p_to_form = par_to_formal(paths_dict['par_out'], paths_dict['formal_par_in'])
            f_par = formal_par(suffix, paths_dict['formal_par_in'], paths_dict['formal_par_run_dir'])
            p_to_time = par_to_timing(paths_dict['par_out'], paths_dict['timing_par_in'])
            t_par = timing_par(suffix, paths_dict['timing_par_in'], paths_dict['timing_par_run_dir'])

            # Physical Verification
            p_to_drc = par_to_drc(paths_dict['par_out'], paths_dict['drc_in'])
            d_node = drc(suffix, paths_dict['drc_in'])
            p_to_lvs = par_to_lvs(paths_dict['par_out'], paths_dict['lvs_in'])
            l_node = lvs(suffix, paths_dict['lvs_in'])

            # --- Explicit Flow Pipeline Interconnect Routing Topology ---
            s_rtl >> s_rtl_to_p >> p_rtl
            
            s_node >> [s_to_sim, s_to_p, s_to_form, s_to_time, s_to_par]
            s_to_sim >> s_syn >> s_syn_to_p
            [s_to_p, s_syn_to_p] >> p_syn
            s_to_form >> f_syn
            s_to_time >> t_syn
            
            s_to_par >> p_node
            p_node >> [p_to_sim, p_to_p, p_to_form, p_to_time, p_to_drc, p_to_lvs]
            p_to_sim >> s_par >> s_par_to_p
            [p_to_p, s_par_to_p] >> p_par
            p_to_form >> f_par
            p_to_time >> t_par
            p_to_drc >> d_node
            p_to_lvs >> l_node

        # Return the par task too so the hierarchical bridge can depend on just
        # this module's par (its par-output-full.json feeds the parent's
        # hier-par-to-syn), NOT on every terminal task of the group.
        return tg, p_node

    start_node = start()
    summary_node = cache_summary_()
    exit_node = exit_()
    notify_node = notify_()
"""

    # 4. Programmatic Inter-Module Routing Map
    if not dependency_graph:
        # Build 1:1 matching directory mapping dictionary literal values
        output += f"""
    paths_{top_module} = {{
        'sim_rtl_run_dir': os.path.join(OBJ_DIR, "sim-rtl-rundir"),
        'power_rtl_run_dir': os.path.join(OBJ_DIR, "power-rtl-rundir"),
        'syn_run_dir': os.path.join(OBJ_DIR, "syn-rundir"),
        'sim_syn_run_dir': os.path.join(OBJ_DIR, "sim-syn-rundir"),
        'power_syn_run_dir': os.path.join(OBJ_DIR, "power-syn-rundir"),
        'par_run_dir': os.path.join(OBJ_DIR, "par-rundir"),
        'sim_par_run_dir': os.path.join(OBJ_DIR, "sim-par-rundir"),
        'power_par_run_dir': os.path.join(OBJ_DIR, "power-par-rundir"),
        'drc_run_dir': os.path.join(OBJ_DIR, "drc-rundir"),
        'lvs_run_dir': os.path.join(OBJ_DIR, "lvs-rundir"),
        'formal_syn_run_dir': os.path.join(OBJ_DIR, "formal-syn-rundir"),
        'formal_par_run_dir': os.path.join(OBJ_DIR, "formal-par-rundir"),
        'timing_syn_run_dir': os.path.join(OBJ_DIR, "timing-syn-rundir"),
        'timing_par_run_dir': os.path.join(OBJ_DIR, "timing-par-rundir"),
        'p_sim_rtl_in': PROJ_CONFIGS,
        'sim_rtl_out': os.path.join(os.path.join(OBJ_DIR, "sim-rtl-rundir"), "sim-output-full.json"),
        'power_sim_rtl_in': os.path.join(OBJ_DIR, "power-sim-rtl-input.json"),
        'power_rtl_out': os.path.join(os.path.join(OBJ_DIR, "power-rtl-rundir"), "power-output-full.json"),
        'p_syn_in': PROJ_CONFIGS,
        'syn_out': os.path.join(os.path.join(OBJ_DIR, "syn-rundir"), "syn-output-full.json"),
        'sim_syn_in': os.path.join(OBJ_DIR, "sim-syn-input.json"),
        'sim_syn_out': os.path.join(os.path.join(OBJ_DIR, "sim-syn-rundir"), "sim-output-full.json"),
        'power_sim_syn_in': os.path.join(OBJ_DIR, "power-sim-syn-input.json"),
        'power_syn_in': os.path.join(OBJ_DIR, "power-syn-input.json"),
        'power_syn_out': os.path.join(os.path.join(OBJ_DIR, "power-syn-rundir"), "power-output-full.json"),
        'par_in': os.path.join(OBJ_DIR, "par-input.json"),
        'par_out': os.path.join(os.path.join(OBJ_DIR, "par-rundir"), "par-output-full.json"),
        'sim_par_in': os.path.join(OBJ_DIR, "sim-par-input.json"),
        'sim_par_out': os.path.join(os.path.join(OBJ_DIR, "sim-par-rundir"), "sim-output-full.json"),
        'power_sim_par_in': os.path.join(OBJ_DIR, "power-sim-par-input.json"),
        'power_par_in': os.path.join(OBJ_DIR, "power-par-input.json"),
        'power_par_out': os.path.join(os.path.join(OBJ_DIR, "power-par-rundir"), "power-output-full.json"),
        'drc_in': os.path.join(OBJ_DIR, "drc-input.json"),
        'drc_out': os.path.join(os.path.join(OBJ_DIR, "drc-rundir"), "drc-output-full.json"),
        'lvs_in': os.path.join(OBJ_DIR, "lvs-input.json"),
        'lvs_out': os.path.join(os.path.join(OBJ_DIR, "lvs-rundir"), "lvs-output-full.json"),
        'formal_syn_in': os.path.join(OBJ_DIR, "formal-syn-input.json"),
        'formal_syn_out': os.path.join(os.path.join(OBJ_DIR, "formal-syn-rundir"), "formal-output-full.json"),
        'formal_par_in': os.path.join(OBJ_DIR, "formal-par-input.json"),
        'formal_par_out': os.path.join(os.path.join(OBJ_DIR, "formal-par-rundir"), "formal-output-full.json"),
        'timing_syn_in': os.path.join(OBJ_DIR, "timing-syn-input.json"),
        'timing_syn_out': os.path.join(os.path.join(OBJ_DIR, "timing-syn-rundir"), "timing-output-full.json"),
        'timing_par_in': os.path.join(OBJ_DIR, "timing-par-input.json"),
        'timing_par_out': os.path.join(os.path.join(OBJ_DIR, "timing-par-rundir"), "timing-output-full.json")
    }}
    mod_tg, _ = create_module_pipeline('{top_module}', '', paths_{top_module})
    start_node >> mod_tg >> exit_node
    mod_tg >> summary_node >> exit_node
    # notify_ hangs off summary_node as well as exit_node on purpose. exit_ is
    # NONE_FAILED, so Airflow marks it upstream_failed the moment any one stage
    # fails, without waiting for the others -- hanging notify_ off it alone
    # sends the completion mail while later stages are still running.
    # summary_node is ALL_DONE behind the whole module group, so it is the one
    # that genuinely waits for everything.
    [summary_node, exit_node] >> notify_node
"""
    else:
        output += "    pipelines = {}\n"
        output += "    pars = {}\n"

        # Build out structural path sets for every individual macro block in the graph
        for node, edges in dependency_graph.items():
            out_edges = edges[1]
            
            p_syn_in_expression = "PROJ_CONFIGS"
            if len(out_edges) > 0:
                p_syn_in_expression = f"[os.path.join(OBJ_DIR, 'syn-{node}-input.json')]"

            output += f"""
    paths_{node} = {{
        'sim_rtl_run_dir': os.path.join(OBJ_DIR, "sim-rtl-{node}"),
        'power_rtl_run_dir': os.path.join(OBJ_DIR, "power-rtl-{node}"),
        'syn_run_dir': os.path.join(OBJ_DIR, "syn-{node}"),
        'sim_syn_run_dir': os.path.join(OBJ_DIR, "sim-syn-{node}"),
        'power_syn_run_dir': os.path.join(OBJ_DIR, "power-syn-{node}"),
        'par_run_dir': os.path.join(OBJ_DIR, "par-{node}"),
        'sim_par_run_dir': os.path.join(OBJ_DIR, "sim-par-{node}"),
        'power_par_run_dir': os.path.join(OBJ_DIR, "power-par-{node}"),
        'drc_run_dir': os.path.join(OBJ_DIR, "drc-{node}"),
        'lvs_run_dir': os.path.join(OBJ_DIR, "lvs-{node}"),
        'formal_syn_run_dir': os.path.join(OBJ_DIR, "formal-syn-{node}"),
        'formal_par_run_dir': os.path.join(OBJ_DIR, "formal-par-{node}"),
        'timing_syn_run_dir': os.path.join(OBJ_DIR, "timing-syn-{node}"),
        'timing_par_run_dir': os.path.join(OBJ_DIR, "timing-par-{node}"),
        'p_sim_rtl_in': PROJ_CONFIGS,
        'sim_rtl_out': os.path.join(os.path.join(OBJ_DIR, "sim-rtl-{node}"), "sim-output-full.json"),
        'power_sim_rtl_in': os.path.join(OBJ_DIR, "power-sim-rtl-{node}-input.json"),
        'power_rtl_out': os.path.join(os.path.join(OBJ_DIR, "power-rtl-{node}"), "power-output-full.json"),
        'p_syn_in': {p_syn_in_expression},
        'syn_out': os.path.join(os.path.join(OBJ_DIR, "syn-{node}"), "syn-output-full.json"),
        'sim_syn_in': os.path.join(OBJ_DIR, "sim-syn-{node}-input.json"),
        'sim_syn_out': os.path.join(os.path.join(OBJ_DIR, "sim-syn-{node}"), "sim-output-full.json"),
        'power_sim_syn_in': os.path.join(OBJ_DIR, "power-sim-syn-{node}-input.json"),
        'power_syn_in': os.path.join(OBJ_DIR, "power-syn-{node}-input.json"),
        'power_syn_out': os.path.join(os.path.join(OBJ_DIR, "power-syn-{node}"), "power-output-full.json"),
        'par_in': os.path.join(OBJ_DIR, "par-{node}-input.json"),
        'par_out': os.path.join(os.path.join(OBJ_DIR, "par-{node}"), "par-output-full.json"),
        'sim_par_in': os.path.join(OBJ_DIR, "sim-par-{node}-input.json"),
        'sim_par_out': os.path.join(os.path.join(OBJ_DIR, "sim-par-{node}"), "sim-output-full.json"),
        'power_sim_par_in': os.path.join(OBJ_DIR, "power-sim-par-{node}-input.json"),
        'power_par_in': os.path.join(OBJ_DIR, "power-par-{node}-input.json"),
        'power_par_out': os.path.join(os.path.join(OBJ_DIR, "power-par-{node}"), "power-output-full.json"),
        'drc_in': os.path.join(OBJ_DIR, "drc-{node}-input.json"),
        'drc_out': os.path.join(os.path.join(OBJ_DIR, "drc-{node}"), "drc-output-full.json"),
        'lvs_in': os.path.join(OBJ_DIR, "lvs-{node}-input.json"),
        'lvs_out': os.path.join(os.path.join(OBJ_DIR, "lvs-{node}"), "lvs-output-full.json"),
        'formal_syn_in': os.path.join(OBJ_DIR, "formal-syn-{node}-input.json"),
        'formal_syn_out': os.path.join(os.path.join(OBJ_DIR, "formal-syn-{node}"), "formal-output-full.json"),
        'formal_par_in': os.path.join(OBJ_DIR, "formal-par-{node}-input.json"),
        'formal_par_out': os.path.join(os.path.join(OBJ_DIR, "formal-par-{node}"), "formal-output-full.json"),
        'timing_syn_in': os.path.join(OBJ_DIR, "timing-syn-{node}-input.json"),
        'timing_syn_out': os.path.join(os.path.join(OBJ_DIR, "timing-syn-{node}"), "timing-output-full.json"),
        'timing_par_in': os.path.join(OBJ_DIR, "timing-par-{node}-input.json"),
        'timing_par_out': os.path.join(os.path.join(OBJ_DIR, "timing-par-{node}"), "timing-output-full.json")
    }}
"""

        # Pass 1: create every module's pipeline up front. A parent can appear
        # before its children in dict-iteration order, so all pipelines must
        # exist before any hierarchical wiring references them (otherwise the
        # parent's `pipelines['child']` lookup KeyErrors at DAG-parse time).
        for node, edges in dependency_graph.items():
            output += "    pipelines['{0}'], pars['{0}'] = create_module_pipeline('{0}', '-{0}', paths_{0})\n".format(node)

        # Pass 2: leaf modules (no children) hang directly off start.
        for node, edges in dependency_graph.items():
            if len(edges[1]) == 0:
                output += "    start_node >> pipelines['{0}']\n".format(node)

        # Pass 3: non-leaf modules get a hier-par-to-syn bridge fed by their
        # children's pipelines (all of which now exist from Pass 1).
        for node, edges in dependency_graph.items():
            out_edges = edges[1]
            if len(out_edges) > 0:
                child_arr = ", ".join([f"pars['{x}']" for x in out_edges])
                out_confs_list = ", ".join([f"os.path.join(OBJ_DIR, 'par-{x}', 'par-output-full.json')" for x in out_edges])

                output += f"""
    # Hierarchical Assembly for macro: {node}
    pstring_{node} = [{out_confs_list}]
    syn_deps_{node} = os.path.join(OBJ_DIR, "syn-{node}-input.json")
    hier_bridge_{node} = hier_par_to_syn.override(task_id='hier_par_to_syn_{node}')(pstring=pstring_{node}, syn_deps=syn_deps_{node})
    [{child_arr}] >> hier_bridge_{node} >> pipelines['{node}']
"""
        
        all_nodes = list(dependency_graph.keys())
        output += "    # Route all workflow paths safely to exit entrypoint\n"
        output += "    exit_drivers = [pipelines[x] for x in {0}]\n".format(all_nodes)
        output += "    exit_drivers >> exit_node\n"
        output += "    exit_drivers >> summary_node\n"
        output += "    summary_node >> exit_node\n"
        output += "    [summary_node, exit_node] >> notify_node\n"

    output += "\ndag_instance = hammer_dag()\n"

    # fingerprint the body so a later rebuild can tell generated from edited
    output += ("\n# sledgehammer-generated-sha256: "
               + hashlib.sha256(output.encode("utf-8")).hexdigest() + "\n")
    _preserve_hand_edits(dag_file, output)
    with open(dag_file, "w") as f:
        f.write(output)

    _link_dag_into_dags_folder(driver, dag_file, unique_dag_id)

    return dependency_graph


def _preserve_hand_edits(dag_file: str, new_text: str) -> None:
    """Never silently discard a DAG someone edited by hand.

    Generated DAGs carry a fingerprint of their own body on the last line.
    If the file on disk no longer matches its fingerprint, a human changed it
    -- a custom step, an extra hook, a tweaked trigger rule -- and a rebuild
    would erase that work. Copy it aside first and say where it went, loudly
    enough to notice in a make log. An unchanged (or absent) file just gets
    replaced as before.
    """
    if not os.path.exists(dag_file):
        return
    try:
        old = open(dag_file).read()
    except OSError:
        return
    marker = "# sledgehammer-generated-sha256: "
    idx = old.rfind(marker)
    if idx != -1:
        tail = old[idx + len(marker):]
        recorded = tail.strip().split()[0] if tail.strip() else ""
        body = old[:idx]
        # the whole file must be exactly body + marker + hash + newline;
        # anything appended after the fingerprint counts as an edit too.
        # The generator hashes the content and then appends "\n" + marker,
        # so the newline just before the marker is a separator, not part of
        # what was hashed.
        content = body[:-1] if body.endswith("\n") else body
        expected = body + marker + recorded + "\n"
        if old == expected and \
                hashlib.sha256(content.encode("utf-8")).hexdigest() == recorded:
            return          # untouched since generation: safe to overwrite
    elif old == new_text:
        return              # pre-fingerprint file, identical anyway
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{dag_file}.hand-edited-{stamp}"
    try:
        shutil.copy2(dag_file, backup)
    except OSError:
        return
    print("=" * 72)
    print(f"NOTE: {os.path.basename(dag_file)} had hand edits; they are NOT in the")
    print(f"      regenerated DAG. Saved your version to:")
    print(f"        {backup}")
    print(f"      Re-apply anything you still need from it.")
    print("=" * 72)


def _link_dag_into_dags_folder(driver, dag_file: str, dag_id: str) -> None:
    """Symlink the generated DAG into a watched dags folder so it is
    triggerable immediately, with no manual publish step.

    The folder is resolved from, in order: the vlsi.core.airflow_dags_folder
    config key, the HAMMER_DAGS_FOLDER env var, then this checkout's own
    dags/ directory (next to the hammer package). The link is named after the
    dag_id, and a symlink rather than a copy so every regeneration is served
    fresh. Failures only warn -- generation itself already succeeded.
    """
    dags_folder = None
    try:
        dags_folder = driver.database.get_setting("vlsi.core.airflow_dags_folder")
    except Exception:
        pass
    if not dags_folder:
        dags_folder = os.environ.get("HAMMER_DAGS_FOLDER")
    try:
        if not dags_folder:
            # This module lives at <checkout>/hammer/vlsi/hammer_build_systems.py,
            # so three dirnames up is the checkout root. (hammer.__file__ is None
            # under editable/namespace installs -- don't use the package attribute.)
            repo_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            dags_folder = os.path.join(repo_root, "dags")
        os.makedirs(dags_folder, exist_ok=True)
        link = os.path.join(dags_folder, dag_id + ".py")
        if os.path.islink(link):
            old = os.path.realpath(link)
            if old != os.path.realpath(dag_file) and os.path.exists(old):
                print(f"NOTE: {dag_id} now builds {os.path.dirname(dag_file)} instead of "
                      f"{os.path.dirname(old)}. Builds whose obj_dir has the same name share "
                      f"one DAG id, so regenerate that one to switch back.")
            os.remove(link)
        elif os.path.lexists(link):
            backup = f"{link}.replaced-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
            os.rename(link, backup)
            print(f"NOTE: {link} was not a link from an earlier build; moved it to {backup}.")
        os.symlink(os.path.abspath(dag_file), link)
        print(f"Linked DAG into dags folder: {link} -> {dag_file}")
    except Exception as e:
        print(f"WARNING: could not link the DAG into a dags folder ({e}). "
              f"Link it by hand:  ln -sfn {dag_file} <dags_folder>/{dag_id}.py")


BuildSystems = {
    # Legacy Make flow -> emits hammer.d. "legacy" is a friendly alias.
    "make":         build_makefile,
    "legacy":       build_makefile,
    # Plain legacy Hammer -> the same hammer.d, and every action runs as upstream
    # Hammer with all SledgeHammer features off (see legacy_hammer.py).
    "hammer":       build_makefile,
    # SledgeHammer Airflow flow -> emits hammer_dag.py. "sledgehammer" alias.
    "airflow":      build_airflow_dag,
    "sledgehammer": build_airflow_dag,
    "none":         build_noop,
}  # type: Dict[str, Callable[[HammerDriver, Callable[[str], None]], dict]]
