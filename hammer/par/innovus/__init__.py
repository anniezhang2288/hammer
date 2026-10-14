#  hammer-vlsi plugin for Cadence Innovus.
#
#  See LICENSE for licence details.

import shutil
from typing import List, Dict, Optional, Callable, Tuple, Any, cast
from itertools import chain

import os
import errno
import re

from hammer.utils import get_or_else, optional_map
from hammer.vlsi import HammerTool, HammerPlaceAndRouteTool, HammerToolStep, HammerToolHookAction, \
    PlacementConstraintType, HierarchicalMode, ILMStruct, ObstructionType, Margins, Supply, PlacementConstraint, MMMCCornerType
from hammer.vlsi.units import CapacitanceValue
from hammer.logging import HammerVLSILogging
import hammer.tech as hammer_tech
from hammer.tech import RoutingDirection
from hammer.tech.specialcells import CellType
from decimal import Decimal
from hammer.common.cadence import CadenceTool

# Notes: camelCase commands are the old syntax (deprecated)
# snake_case commands are the new/common UI syntax.
# This plugin should only use snake_case commands.

class InnovusTclToPythonConverter:
    """Converts Innovus TCL scripts to Python following Innovus 25.1 Python API rules"""
    
    # Python reserved keywords that need _option_ prefix
    RESERVED_KEYWORDS = {
        'False', 'break', 'for', 'not', 'None', 'class', 'from', 'or', 
        'True', 'continue', 'global', 'pass', '__peg_parser__', 'def', 'if', 'raise', 
        'and', 'del', 'import', 'return', 'as', 'elif', 'in', 'try', 
        'assert', 'else', 'is', 'while', 'async', 'except', 'lambda', 
        'with', 'await', 'finally', 'nonlocal', 'yield'
    }
    
    # Commands that should use tcl.eval() to avoid issues
    FALLBACK_TO_TCL = {
        'source',    # Source TCL files
    }
    
    # Commands where output file is positional (not named)
    POSITIONAL_OUTPUT_CMDS = {
        'write_db': True,
        'write_stream': True,
        'write_sdf': True,
        'write_def': True,
        'write_netlist': True,
        'write_parasitics': True
    }
    
    def __init__(self):
        self.output_lines = []

    def _is_register_file_io_block(self, lines: List[str], start_idx: int) -> Tuple[bool, int]:
        """
        Detect register file I/O blocks - these must be wrapped entirely in tcl.eval()
        
        Pattern to match:
            set VAR "./find_regs_*.json"
            set VAR [open $VAR "w"]
            puts $VAR ...
            ... (complex TCL code including for-loops)
            close $VAR
        """
        if start_idx >= len(lines):
            return False, start_idx
        
        line = lines[start_idx].strip()
        
        # Must start with 'set ' (after stripping whitespace)
        if not line.startswith('set '):
            return False, start_idx
        
        # Must contain .json
        if '.json' not in line:
            return False, start_idx
        
        # Must be a find_regs file (cells, paths, or regs)
        if 'find_regs' not in line:
            return False, start_idx
        
        # Extract variable name
        parts = line.split()
        if len(parts) < 3:
            return False, start_idx
        
        var_name = parts[1]
        
        # Search for matching close statement
        # Increased range to 30 lines to handle larger blocks
        end_idx = start_idx + 1
        found_close = False
        
        while end_idx < len(lines) and (end_idx - start_idx) < 30:
            current = lines[end_idx].strip()
            
            # Look for: close $VAR or close VAR
            if current.startswith('close '):
                # Check if variable name appears in close statement
                if var_name in current or f'${var_name}' in current:
                    found_close = True
                    break
            
            end_idx += 1
        
        # DEBUG: Uncomment to trace detection
        # if found_close:
        #     print(f"DEBUG: Detected file I/O block: '{var_name}' from line {start_idx+1} to {end_idx+1}")
        
        return found_close, end_idx if found_close else start_idx


    def _wrap_block_in_tcl_eval(self, lines: List[str], start_idx: int, end_idx: int) -> str:
        """Wrap TCL block in tcl.eval() with proper backslash handling"""
        block_lines = []
        
        for i in range(start_idx, end_idx + 1):
            line = lines[i].rstrip()
            if line:
                block_lines.append(line)
        
        tcl_block = '\n'.join(block_lines)
        
        # Use raw string (r'''...''') to preserve backslashes!
        return f"tcl.eval(r'''{tcl_block}''')"  # ← Note the 'r' prefix!

        
    def convert_tcl_to_python(self, tcl_content: str, include_header: bool = True) -> str:
        """Convert TCL content to Python string"""
        self.output_lines = []
        
        # Add header only when generating a top-level file
        if include_header:
            self.output_lines.append("# " + "=" * 78)
            self.output_lines.append("# Auto-converted from TCL to Python for Innovus 25.1")
            self.output_lines.append("# " + "=" * 78)
            self.output_lines.append("")
        
        tcl_lines = tcl_content.split('\n')
        
        i = 0
        while i < len(tcl_lines):
            line = tcl_lines[i].rstrip()
            
            # Check if this is the start of a register file I/O block
            is_file_io, end_idx = self._is_register_file_io_block(tcl_lines, i)
            
            if is_file_io:
                # Wrap the entire block in tcl.eval()
                self.output_lines.append("# Complex file I/O block - using tcl.eval()")
                tcl_eval_line = self._wrap_block_in_tcl_eval(tcl_lines, i, end_idx)
                self.output_lines.append(tcl_eval_line)
                self.output_lines.append("")
                i = end_idx + 1
                continue
            
            # Handle multi-line commands
            full_line = line
            while i + 1 < len(tcl_lines) and (line.endswith('\\\\') or self._has_unclosed_braces(full_line)):
                i += 1
                line = tcl_lines[i].rstrip()
                full_line += ' ' + line.lstrip()
            
            converted = self.convert_line(full_line)
            if converted:
                self.output_lines.append(converted)
            
            i += 1
        
        return '\n'.join(self.output_lines)
    
    def _has_unclosed_braces(self, line: str) -> bool:
        """Check if line has unclosed braces"""
        open_count = line.count('{')
        close_count = line.count('}')
        return open_count > close_count
    
    def convert_line(self, line: str) -> str:
        """Convert a single TCL line to Python"""
        line = line.strip()
        
        # Skip empty lines and pure comments
        if not line or line.startswith('#'):
            if line.startswith('#'):
                return line
            return ''
        
        # Handle 'puts' statements
        if line.startswith('puts '):
            return self._convert_puts(line)
        
        # Handle 'ln' shell command -> os.symlink()
        if line.startswith('ln '):
            return self._convert_ln(line)
        
        # Handle 'exit' -> sys.exit()
        if line.strip() == 'exit':
            return 'sys.exit()'
        
        # Handle 'close' command
        if line.startswith('close '):
            var_name = line.split()[1].strip('$')
            return f"{var_name}.close()"
        
        # Handle 'open' command
        if 'open ' in line and ('[open' in line or line.startswith('set ')):
            return self._convert_file_open(line)
        
        # source -> source() Python call
        cmd_name = self._extract_command_name(line)
        if cmd_name == 'source':
            return self._convert_source(line)
        
        # Handle set_db commands
        if line.startswith('set_db '):
            return self._convert_set_db(line)
        
        # Plain Tcl variable assignment: keep it at the Tcl level (before the
        # get_db branch, whose lossy filter translation would otherwise grab
        # `set var [get_db ...]` and strand later $var references).
        if line.startswith('set '):
            return self._convert_variable_assignment(line)

        # Handle get_db -> db() conversions
        if 'get_db' in line:
            return self._convert_get_db_line(line)
        
        # Handle regular commands
        if self._is_command(line):
            return self._convert_command(line)
        
        # Handle TCL control structures - these are emitted by tech plugin hooks
        # and should have been converted already; emit as comment to flag for review
        if line.startswith('if {') or line.startswith('for {') or line.startswith('while {'):
            return f"# WARNING: unconverted TCL control structure: {line}"
        
        # Handle variable assignments
        if line.startswith('set '):
            return self._convert_variable_assignment(line)
        
        # Default fallback: treat as a direct Python function call
        # (all Innovus TCL commands have Python equivalents with the same name)
        return self._convert_command(line)

    # TCL control-flow words whose bodies have no line-by-line Python
    # equivalent; the whole construct goes to the live Tcl interp instead.
    _TCL_CONTROL = {'if', 'foreach', 'while', 'for', 'proc', 'switch', 'catch'}

    def convert_block(self, cmd: str) -> List[str]:
        """Convert a chunk of TCL (possibly many lines) to Python lines.

        Balanced single commands go through convert_line as before. A
        multi-line control construct (a foreach/if/while body spanning
        lines) cannot be converted one line at a time -- doing so emits the
        header as a bogus call, converts the body out of context, and leaves
        an orphan closing brace. Instead the brace balance is tracked and a
        whole balanced block is handed to the Tcl interpreter via tcl.eval,
        which Innovus's Python engine keeps alive for exactly this.
        """
        out: List[str] = []
        block: List[str] = []
        pending = ""
        depth = 0
        for raw in cmd.splitlines():
            line = raw.strip()
            if not line:
                continue
            if depth > 0:
                block.append(raw.rstrip())
                depth += self._brace_delta(line)
                if depth <= 0:
                    out.append("tcl.eval(" + repr("\n".join(block)) + ")")
                    block = []
                    depth = 0
                continue
            if line.startswith('#'):
                out.append(line)
                continue
            if line.endswith('\\') and not line.endswith('\\\\'):
                pending += line[:-1].rstrip() + ' '
                continue
            if pending:
                line = pending + line
                pending = ""
            first = line.split(None, 1)[0]
            delta = self._brace_delta(line)
            if first in self._TCL_CONTROL or delta > 0:
                if delta > 0:
                    block = [line]
                    depth = delta
                else:
                    out.append("tcl.eval(" + repr(line) + ")")
                continue
            converted = self.convert_line(line)
            if converted:
                out.append(converted)
        if pending:
            converted = self.convert_line(pending.rstrip())
            if converted:
                out.append(converted)
        if block:
            # unbalanced input; hand it to Tcl rather than dropping it
            out.append("tcl.eval(" + repr("\n".join(block)) + ")")
        return out

    @staticmethod
    def _brace_delta(line: str) -> int:
        """Net curly-brace balance of a line, ignoring escaped braces."""
        s = line.replace('\\\\', '').replace('\\{', '').replace('\\}', '')
        return s.count('{') - s.count('}')

    def _convert_puts(self, line: str) -> str:
        """Convert TCL puts to Python print"""
        if line.startswith('puts "'):
            content = line[6:-1]
            content = content.replace('"', '\\"')
            return f'print("{content}")'
        
        match = re.match(r'puts\s+(.+)', line)
        if match:
            var_expr = match.group(1).strip()
            
            # Handle file handle: puts $file_handle "content"
            if ' "' in var_expr or " '" in var_expr:
                parts = var_expr.split(maxsplit=1)
                file_var = parts[0].strip('$')
                content = parts[1].strip('"\'')
                content = content.replace('"', '\\"')
                return f'{file_var}.write("{content}\\n")'
            
            if var_expr.startswith('$'):
                var_name = var_expr[1:]
                return f'print({var_name})'
            
            return f'print({var_expr})'
        
        return f"print({line[5:]})"
    
    def _convert_file_open(self, line: str) -> str:
        """Convert TCL file open to Python open()"""
        match = re.match(r'set\s+(\w+)\s+\[open\s+"([^"]+)"\s+"([^"]+)"\]', line)
        if match:
            var_name = match.group(1)
            filename = match.group(2)
            mode = match.group(3)
            return f'{var_name} = open("{filename}", "{mode}")'
        
        match = re.match(r'set\s+(\w+)\s+\[open\s+\$(\w+)\s+"([^"]+)"\]', line)
        if match:
            var_name = match.group(1)
            filename_var = match.group(2)
            mode = match.group(3)
            return f'{var_name} = open({filename_var}, "{mode}")'
        
        # Unrecognized open pattern - best effort: call as command
        return self._convert_command(line)
    
    def _convert_source(self, line: str) -> str:
        """Convert TCL source command to Python source() call."""
        # source -echo -verbose /path/to/file.tcl
        parts = line.split()
        path = parts[-1]  # path is always last
        has_echo = '-echo' in parts
        has_verbose = '-verbose' in parts
        if has_echo and has_verbose:
            return f"source({path!r}, echo=True, verbose=True)"
        elif has_echo:
            return f"source({path!r}, echo=True)"
        elif has_verbose:
            return f"source({path!r}, verbose=True)"
        else:
            return f"source({path!r})"

    def _convert_ln(self, line: str) -> str:
        """Convert TCL/shell ln command to os.system()."""
        return f"os.system({line!r})"

    def _extract_command_name(self, line: str) -> str:
        """Extract the command name from a line"""
        parts = line.split()
        if parts:
            return parts[0]
        return ''
    
    def _is_command(self, line: str) -> bool:
        """Check if line is a Innovus command"""
        tcl_keywords = {'if', 'for', 'while', 'foreach', 'proc', 'set'}
        cmd = self._extract_command_name(line)
        return cmd and cmd not in tcl_keywords
    
    def _convert_set_db(self, line: str) -> str:
        """Convert set_db commands to Python db() attribute access"""
        match = re.match(r'set_db\s+(\S+)\s+(.+)', line)
        if match:
            attr_name = match.group(1)
            if attr_name.startswith('$') or attr_name.startswith('['):
                # set_db on an object list held in a Tcl variable (set_db
                # $macros .place_status fixed) has no attribute-path Python
                # form; keep it at the Tcl level.
                return f"tcl.eval({line!r})"
            value = match.group(2).strip()
            py_value = self._convert_value(value)
            return f"db().{attr_name} = {py_value}"
        
        return f"# FIXME: Could not convert: {line}"
    
    def _convert_get_db_line(self, line: str) -> str:
        """Convert lines containing get_db to use db() API"""
        # Pattern: set var [get_db [get_db obj_type -if expr] .attr]
        # e.g. set refs [get_db [get_db lib_cells -if .is_sequential==true] .base_name]
        nested_match = re.match(
            r'set\s+(\w+)\s+\[get_db\s+\[get_db\s+(\w+)\s+-if\s+\.(\w+)==(\w+)\]\s+\.(\w+)\]', line)
        if nested_match:
            var_name    = nested_match.group(1)
            obj_type    = nested_match.group(2)
            attr_name   = nested_match.group(3)
            attr_val    = nested_match.group(4)
            result_attr = nested_match.group(5)
            # Convert TCL true/false to Python
            py_val = 'True' if attr_val.lower() == 'true' else ('False' if attr_val.lower() == 'false' else attr_val)
            return f"{var_name} = db().{obj_type}().filter(lambda x: x.{attr_name} == {py_val}).{result_attr}"

        # Pattern: set var [get_db objects -if .attr==val]
        if_match = re.match(r'set\s+(\w+)\s+\[get_db\s+(\w+)\s+-if\s+\.(\w+)==(\w+)\]', line)
        if if_match:
            var_name  = if_match.group(1)
            obj_type  = if_match.group(2)
            attr_name = if_match.group(3)
            attr_val  = if_match.group(4)
            py_val = 'True' if attr_val.lower() == 'true' else ('False' if attr_val.lower() == 'false' else attr_val)
            return f"{var_name} = db().{obj_type}().filter(lambda x: x.{attr_name} == {py_val})"

        # Pattern: set var [get_db objects pattern]
        match = re.match(r'set\s+(\w+)\s+\[get_db\s+(\w+)\s+([^\]]+)\]', line)
        if match:
            var_name = match.group(1)
            obj_type = match.group(2)
            pattern = match.group(3).strip()
            
            if '-if' in pattern:
                # Complex -if filter: use db().filter() with lambda
                # e.g. get_db lib_cells -if .is_sequential==true
                if_match = re.search(r'-if\s+["\']?\.(\w+)==(\w+)["\']?', pattern)
                if if_match:
                    attr, val = if_match.group(1), if_match.group(2)
                    pyval = 'True' if val.lower() == 'true' else ('False' if val.lower() == 'false' else repr(val))
                    return f"{var_name} = db().{obj_type}().filter(lambda x: x.{attr} == {pyval})"
                # Fall through to direct db() call with pattern
                pat = pattern.split('-if')[0].strip().strip('"\'{}')
                return f"{var_name} = db().{obj_type}('{pat}').filter(lambda x: True)  # TODO: check filter"
            
            if pattern.startswith('{') and pattern.endswith('}'):
                pattern = pattern[1:-1]
            
            pattern = pattern.strip('"\'')
            return f"{var_name} = db().{obj_type}('{pattern}')"
        
        if '[get_db' in line:
            # Inline get_db in a command - convert to _convert_command which handles it
            return self._convert_command(line)
        
        return line
    
    def _convert_command(self, line: str) -> str:
        """Convert a TCL command to Python function call"""
        parts = self._tokenize_command(line)
        if not parts:
            return f"# FIXME: Empty command"

        cmd_name = parts[0]

        # A Tcl variable assignment that fell through the specific set-handlers
        # above. Keep the variable at Tcl level via the bridge (Python's builtin
        # set() would swallow it), so later $var references resolve.
        if cmd_name == 'set' and len(parts) == 3:
            return f"tcl.eval({line!r})"
        positional_args = []
        named_args = {}
        
        i = 1
        while i < len(parts):
            part = parts[i]
            
            if part.startswith('-'):
                flag_name = part[1:]
                
                # Numeric flags like -5.8, -181 are illegal as keyword args;
                # pass them as positional string arguments e.g. '-5.8'
                if flag_name.replace('.', '', 1).lstrip('-').isdigit() or \
                   (flag_name and flag_name[0].isdigit()):
                    positional_args.append(f"'{part}'")
                    i += 1
                    continue
                
                # A following token that is a negative number (-start_offset -0.200)
                # is this flag's value, not another flag.
                if i + 1 >= len(parts) or (parts[i + 1].startswith('-')
                                           and not re.fullmatch(r'-(\d+\.?\d*|\.\d+)', parts[i + 1])):
                    if flag_name in self.RESERVED_KEYWORDS:
                        flag_name = f'_option_{flag_name}'
                    named_args[flag_name] = True
                    i += 1
                else:
                    flag_value = parts[i + 1]
                    
                    if flag_name in self.RESERVED_KEYWORDS:
                        flag_name = f'_option_{flag_name}'
                    
                    named_args[flag_name] = self._convert_value(flag_value)
                    i += 2
            else:
                if part.startswith('{'):
                    # A braced list as a positional argument has no reliable
                    # Python calling convention: the bridge glues the list
                    # onto the command name without a space (set_dont_use{...})
                    # and Tcl errors out. Keep the whole command in Tcl.
                    return f"tcl.eval({line!r})"
                positional_args.append(self._convert_value(part))
                i += 1
        
        # Special handling for commands with positional output files
        if cmd_name in self.POSITIONAL_OUTPUT_CMDS:
            if 'output' in named_args:
                output_val = named_args.pop('output')
                positional_args.insert(0, output_val)
        
        # Build Python function call
        py_args = []
        py_args.extend(positional_args)
        
        for key, val in named_args.items():
            if isinstance(val, bool) and val:
                py_args.append(f"{key}=True")
            elif isinstance(val, bool) and not val:
                py_args.append(f"{key}=False")
            else:
                py_args.append(f"{key}={val}")
        
        args_str = ', '.join(py_args)
        return f"{cmd_name}({args_str})"
    
    @staticmethod
    def _brackets_balanced(value: str) -> bool:
        """True when the outermost [ ] of value enclose the whole token, i.e.
        the bracket depth never returns to zero before the final character."""
        depth = 0
        for i, ch in enumerate(value):
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0 and i != len(value) - 1:
                    return False
                if depth < 0:
                    return False
        return depth == 0

    def _tokenize_command(self, line: str) -> List[str]:
        """Tokenize a TCL command into parts, respecting braces, brackets and quotes.

        Square brackets must be tracked like braces: a value such as
        -area [get_computed_shapes [get_db designs .bbox] SIZEY -2.4] is ONE
        word in Tcl, and splitting it on inner whitespace scatters the pieces
        between positional and named args, producing a scrambled command.
        Tokens end only at whitespace (as in Tcl), so glued forms like
        {*}[cmd ...] also stay whole.
        """
        tokens = []
        current_token = ''
        in_braces = 0
        in_brackets = 0
        in_quotes = False
        escape_next = False

        for char in line:
            if escape_next:
                current_token += char
                escape_next = False
                continue

            if char == '\\':
                escape_next = True
                continue

            if char == '"' and in_braces == 0:
                in_quotes = not in_quotes
                current_token += char
                continue

            if char == '{' and not in_quotes:
                in_braces += 1
                current_token += char
                continue

            if char == '}' and not in_quotes:
                in_braces -= 1
                current_token += char
                continue

            if char == '[' and not in_quotes and in_braces == 0:
                in_brackets += 1
                current_token += char
                continue

            if char == ']' and not in_quotes and in_braces == 0:
                in_brackets -= 1
                current_token += char
                continue

            if char in ' \t' and in_braces == 0 and in_brackets == 0 and not in_quotes:
                if current_token:
                    tokens.append(current_token)
                    current_token = ''
                continue

            current_token += char

        if current_token:
            tokens.append(current_token)

        return tokens
    
    def _convert_value(self, value: str) -> str:
        """Convert a TCL value to Python representation"""
        value = value.strip()

        # PYTHON: prefix means pass through as raw Python expression (may be brace-wrapped)
        stripped = value.strip('{}')
        if stripped.startswith('PYTHON:'):
            return stripped[7:]

        # Bracket command substitution -> evaluate through the Tcl bridge. The
        # 25.1 API's bindings brace-quote plain string arguments, so a value
        # left as the literal string '[cmd ...]' reaches the tool unevaluated;
        # and translating to nested Python calls doesn't work either because
        # the API drops some commands as bare names (get_db/set_db become the
        # db() accessor). tcl.eval keeps the original Tcl semantics and hands
        # back the computed result. A leading {*} expansion is dropped: the
        # bridge returns the list whole.
        bexpr = value[3:] if value.startswith('{*}[') else value
        if bexpr.startswith('[') and bexpr.endswith(']') and self._brackets_balanced(bexpr):
            return f"tcl.eval({bexpr[1:-1].strip()!r})"
        
        # Handle braced lists
        if value.startswith('{') and value.endswith('}'):
            inner = value[1:-1].strip()
            items = self._tokenize_list(inner)
            
            if len(items) == 1:
                return self._convert_value(items[0])
            
            converted_items = [self._convert_value(item) for item in items]
            return f"[{', '.join(converted_items)}]"
        
        # Handle quoted strings
        if value.startswith('"') and value.endswith('"'):
            inner = value[1:-1]
            inner = inner.replace("'", "\\'")
            return f"'{inner}'"
        
        # Handle numbers
        if value.replace('.', '').replace('-', '').replace('+', '').isdigit():
            return value
        
        # Handle booleans
        if value.lower() == 'true':
            return 'True'
        if value.lower() == 'false':
            return 'False'
        
        # Handle TCL variables. A plain $name is read back through the bridge:
        # left as a literal string it would reach the tool brace-quoted and
        # never substituted. Compound forms keep the old literal behavior.
        if value.startswith('$'):
            if re.fullmatch(r'\$[A-Za-z_]\w*', value):
                return f"tcl.eval({('set ' + value[1:])!r})"
            return f"'{value}'"
        
        # Default: treat as string
        return f"'{value}'"
    
    def _tokenize_list(self, content: str) -> List[str]:
        """Tokenize a TCL list, preserving complete items even with spaces"""
        tokens = []
        current_token = ''
        in_braces = 0
        in_quotes = False
        escape_next = False
        
        for char in content:
            if escape_next:
                current_token += char
                escape_next = False
                continue
            
            if char == '\\':
                escape_next = True
                current_token += char
                continue
            
            if char == '"' and in_braces == 0:
                in_quotes = not in_quotes
                current_token += char
                continue
            
            if char == '{' and not in_quotes:
                in_braces += 1
                current_token += char
                continue
            
            if char == '}' and not in_quotes:
                in_braces -= 1
                current_token += char
                continue
            
            if char in ' \t\n' and in_braces == 0 and not in_quotes:
                if current_token:
                    tokens.append(current_token)
                    current_token = ''
                continue
            
            current_token += char
        
        if current_token:
            tokens.append(current_token)
        
        return tokens
    
    def _convert_variable_assignment(self, line: str) -> str:
        """Keep a TCL variable assignment at the Tcl level.

        Reads are converted as tcl.eval('set var'), so the variable has to
        exist in the Tcl interpreter; translating the assignment into a
        Python variable would strand every later $var reference (and the
        old bracket-value translation was lossy anyway).
        """
        return f"tcl.eval({line!r})"

class Innovus(HammerPlaceAndRouteTool, CadenceTool):

    def export_config_outputs(self) -> Dict[str, Any]:
        outputs = dict(super().export_config_outputs())
        # TODO(edwardw): find a "safer" way of passing around these settings keys.
        outputs["par.outputs.seq_cells"] = self.output_seq_cells
        outputs["par.outputs.all_regs"] = self.output_all_regs
        outputs["par.outputs.sdf_file"] = self.output_sdf_path
        outputs["par.outputs.def_file"] = self.output_def_path
        outputs["par.outputs.spefs"] = self.output_spef_paths
        return outputs

    def fill_outputs(self) -> bool:
        if self.ran_write_ilm:
            # Check that the ILMs got written.

            ilm_data_dir = "{ilm_dir_name}/mmmc/ilm_data/{top}".format(ilm_dir_name=self.ilm_dir_name,
                                                                       top=self.top_module)
            postRoute_v_gz = os.path.join(ilm_data_dir, "{top}_postRoute.v.gz".format(top=self.top_module))

            if not os.path.isfile(postRoute_v_gz):
                raise ValueError("ILM output postRoute.v.gz %s not found" % (postRoute_v_gz))

            # Copy postRoute.v.gz to postRoute.ilm.v.gz since that's what Genus seems to expect.
            postRoute_ilm_v_gz = os.path.join(ilm_data_dir, "{top}_postRoute.ilm.v.gz".format(top=self.top_module))
            shutil.copyfile(postRoute_v_gz, postRoute_ilm_v_gz)

            # Write output_ilms.
            self.output_ilms = [
                ILMStruct(dir=self.ilm_dir_name, data_dir=ilm_data_dir, module=self.top_module,
                          lef=os.path.join(self.run_dir, "{top}ILM.lef".format(top=self.top_module)),
                          gds=self.output_gds_filename,
                          netlist=self.output_netlist_filename,
                          sim_netlist=self.output_sim_netlist_filename,
                          sdcs=self.output_ilm_sdcs)
            ]
        else:
            self.output_ilms = []

        # Check that the regs paths were written properly if the write_regs step was run
        self.output_seq_cells = self.all_cells_path
        self.output_all_regs = self.all_regs_path
        if self.ran_write_regs:
            if not os.path.isfile(self.all_cells_path):
                raise ValueError("Output find_regs_cells.json %s not found" % (self.all_cells_path))

            if not os.path.isfile(self.all_regs_path):
                raise ValueError("Output find_regs_paths.json %s not found" % (self.all_regs_path))

            if not self.process_reg_paths(self.all_regs_path):
                self.logger.error("Failed to process all register paths")
        else:
            self.logger.info("Did not run write_regs")

        # Check that the par outputs exist if the par run was successful
        self.output_gds = self.output_gds_filename
        self.output_netlist = self.output_netlist_filename
        self.output_physical_netlist = self.output_physical_netlist_filename
        self.output_sim_netlist = self.output_sim_netlist_filename
        self.hcells_list = []
        self.sdf_file = self.output_sdf_path
        self.spef_files = self.output_spef_paths

        if self.ran_write_design:
            if not os.path.isfile(self.output_gds_filename):
                raise ValueError("Output GDS %s not found" % (self.output_gds_filename))

            if not os.path.isfile(self.output_netlist_filename):
                raise ValueError("Output netlist %s not found" % (self.output_netlist_filename))

            if not os.path.isfile(self.output_sim_netlist_filename):
                raise ValueError("Output sim netlist %s not found" % (self.output_sim_netlist_filename))

            if not os.path.isfile(self.output_sdf_path):
                raise ValueError("Output SDF %s not found" % (self.output_sdf_path))

            for spef_path in self.output_spef_paths:
                if not os.path.isfile(spef_path):
                    raise ValueError("Output SPEF %s not found" % (spef_path))
        else:
            self.logger.info("Did not run write_design")

        return True

    @property
    def output_gds_filename(self) -> str:
        return os.path.join(self.run_dir, "{top}.gds".format(top=self.top_module))

    @property
    def output_netlist_filename(self) -> str:
        return os.path.join(self.run_dir, "{top}.lvs.v".format(top=self.top_module))

    @property
    def output_physical_netlist_filename(self) -> str:
        return os.path.join(self.run_dir, "{top}.physical.v".format(top=self.top_module))

    @property
    def output_sim_netlist_filename(self) -> str:
        return os.path.join(self.run_dir, "{top}.sim.v".format(top=self.top_module))

    @property
    def all_regs_path(self) -> str:
        return os.path.join(self.run_dir, "find_regs_paths.json")

    @property
    def all_cells_path(self) -> str:
        return os.path.join(self.run_dir, "find_regs_cells.json")

    @property
    def output_sdf_path(self) -> str:
        return os.path.join(self.run_dir, "{top}.par.sdf".format(top=self.top_module))

    @property
    def output_def_path(self) -> str:
        return os.path.join(self.run_dir, "{top}.def".format(top=self.top_module))

    @property
    def output_spef_paths(self) -> List[str]:
        corners = self.get_mmmc_corners()
        if corners:
            # Order matters in tool consuming spefs (ensured here by get_mmmc_corners())!
            return list(map(lambda c: os.path.join(self.run_dir, "{top}.{corner}.par.spef".format(top=self.top_module, corner=c.name)), corners))
        else:
            return [os.path.join(self.run_dir, "{top}.par.spef".format(top=self.top_module))]

    @property
    def output_ilm_sdcs(self) -> List[str]:
        corners = self.get_mmmc_corners()
        if corners:
            filtered = list(filter(lambda c: c.type in [MMMCCornerType.Setup, MMMCCornerType.Hold], corners))
            ctype_map = {MMMCCornerType.Setup: "setup", MMMCCornerType.Hold: "hold"}
            return list(map(lambda c: os.path.join(self.run_dir, "{top}_postRoute_{corner_name}.{corner_type}_view.core.sdc".format(
                top=self.top_module, corner_name=c.name, corner_type=ctype_map[c.type])), filtered))
        else:
            return [os.path.join(self.run_dir, "{top}_postRoute.core.sdc".format(top=self.top_module))]

    @property
    def env_vars(self) -> Dict[str, str]:
        v = dict(super().env_vars)
        v["INNOVUS_BIN"] = self.get_setting("par.innovus.innovus_bin")
        if self.version() >= self.version_number("221"):  # support critical region resynthesis with DDI
            v["PATH"] = f'{os.environ.copy()["PATH"]}:{os.path.dirname(self.get_setting("par.innovus.innovus_bin").replace("INNOVUS", "GENUS"))}'
        if self.get_setting("par.innovus.signoff"):  # add path to Tempus
            v["PATH"] = v["PATH"] + f':{self.get_setting("cadence.cadence_home")}/SSV/SSV{self.get_setting("par.innovus.version")}/bin'
        return v

    @property
    def _step_transitions(self) -> List[Tuple[str, str]]:
        """
        Private helper property to keep track of which steps we ran so that we
        can create symlinks.
        This is a list of (pre, post) steps
        """
        return self.attr_getter("__step_transitions", [])

    @_step_transitions.setter
    def _step_transitions(self, value: List[Tuple[str, str]]) -> None:
        self.attr_setter("__step_transitions", value)

    def do_pre_steps(self, first_step: HammerToolStep) -> bool:
        assert super().do_pre_steps(first_step)
        if first_step != self.first_step:
            if self.use_python:
                # don't trust the console to die on an exception: like TCL,
                # an interactive prompt would hang a batch run forever. A
                # hard exit makes a bad checkpoint cost one fast attempt.
                self.py_append(
                    f"try:\n"
                    f"    read_db('pre_{first_step.name}')\n"
                    f"except Exception as _resume_err:\n"
                    f"    print('ERROR: failed to load checkpoint "
                    f"pre_{first_step.name}: ' + str(_resume_err))\n"
                    f"    import os\n"
                    f"    os._exit(2)")
            else:
                # guard the restore: on a bad checkpoint innovus drops to an
                # interactive prompt instead of exiting, hanging a batch run
                self.append(
                    'if {{[catch {{read_db pre_{step}}} _resume_err]}} {{'
                    'puts $_resume_err; '
                    'puts {{ERROR: failed to load checkpoint pre_{step}}}; exit 2}}'.format(step=first_step.name))
        return True

    def do_between_steps(self, prev: HammerToolStep, next: HammerToolStep) -> bool:
        assert super().do_between_steps(prev, next)
        if self.use_python:
            self.py_append(f"write_db('pre_{next.name}')")
            self.py_append(f"os.system('ln -sfn pre_{next.name} latest')")
        else:
            self.verbose_append("write_db pre_{step}".format(step=next.name))
            self.verbose_append("ln -sfn pre_{step} latest".format(step=next.name))
        self._step_transitions = self._step_transitions + [(prev.name, next.name)]
        return True

    def do_post_steps(self) -> bool:
        assert super().do_post_steps()
        try:
            for prev, next in self._step_transitions:
                os.symlink(
                    os.path.join(self.run_dir, "pre_{next}".format(next=next)),
                    os.path.join(self.run_dir, "post_{prev}".format(prev=prev))
                )
        except OSError as e:
            if e.errno != errno.EEXIST:
                self.logger.warning("Failed to create post_* symlinks: " + str(e))

        if len(self._step_transitions) > 0:
            last = "post_{step}".format(step=self._step_transitions[-1][1])
            if self.use_python:
                self.py_append(f"write_db('{last}')")
                self.py_append(f"os.system('ln -sfn {last} latest')")
            else:
                self.verbose_append("write_db {last}".format(last=last))
                self.verbose_append("ln -sfn {last} latest".format(last=last))

        return self.run_innovus()

    def get_tool_hooks(self) -> List[HammerToolHookAction]:
        return [self.make_persistent_hook(innovus_global_settings)]

    @property
    def steps(self) -> List[HammerToolStep]:
        steps = [
            self.init_design,
            self.floorplan_design,
            self.place_bumps,
            self.place_tap_cells,
            self.power_straps,
            self.place_pins,
            self.place_opt_design,
            self.clock_tree,
            self.add_fillers
        ]
        if self.version() >= self.version_number("231"):
            # Route & post-route opt are now combined
            steps += [self.route_opt_design]
        else:
            steps += [
                self.route_design,
                self.opt_design
            ]
        if self.get_setting("par.innovus.signoff"):
            steps += [self.opt_signoff]
        write_design_step = [
            self.write_regs,
            self.write_design
        ]  # type: List[Callable[[], bool]]
        if self.hierarchical_mode == HierarchicalMode.Flat:
            # Nothing to do
            pass
        elif self.hierarchical_mode == HierarchicalMode.Leaf:
            # All modules in hierarchical must write an ILM
            write_design_step += [self.write_ilm]
        elif self.hierarchical_mode == HierarchicalMode.Hierarchical:
            # All modules in hierarchical must write an ILM
            write_design_step += [self.write_ilm]
        elif self.hierarchical_mode == HierarchicalMode.Top:
            # No need to write ILM at the top.
            # Top needs assemble_design instead.
            steps += [self.assemble_design]
            pass
        else:
            raise NotImplementedError("HierarchicalMode not implemented: " + str(self.hierarchical_mode))
        return self.make_steps_from_methods(steps + write_design_step)

    def tool_config_prefix(self) -> str:
        return "par.innovus"

    # -------------------------------------------------------------------------
    # Python-native script generation for Innovus 25.1+
    # -------------------------------------------------------------------------

    @property
    def use_python(self) -> bool:
        """Whether to emit the Innovus Python API instead of Tcl.

        Off by default: the Python engine needs the INVS200 (AI) license, and
        a site with only the INVS100 base license cannot run the generated
        par.py at all. Turn it on with par.innovus.use_python when the license
        is available; the version still has to be 25.1 or newer, since older
        Innovus has no Python API.
        """
        if not self.get_setting("par.innovus.use_python", nullvalue=False):
            return False
        return self.version() >= self.version_number("251")

    def py_append(self, python_line: str) -> None:
        """Append a raw Python line directly to the output (25.1+ only)."""
        self.output.append(python_line)

    def append(self, cmd: str, clean: bool = False) -> None:
        """Append a command. In Python mode, converts TCL to Python (for external hooks/tech plugins)."""
        if self.use_python:
            self.output.extend(InnovusTclToPythonConverter().convert_block(cmd))
        else:
            super().append(cmd, clean=clean)  # type: ignore

    def verbose_append(self, cmd: str, clean: bool = False) -> None:
        """Append a command. In Python mode, converts TCL to Python."""
        if self.use_python:
            self.output.extend(InnovusTclToPythonConverter().convert_block(cmd))
        else:
            super().verbose_append(cmd, clean=clean)  # type: ignore

    def init_design(self) -> bool:
        # Perform common path pessimism removal in setup and hold mode
        if self.use_python:
            self.py_append("db().timing_analysis_cppr = 'both'")
            self.py_append("db().timing_analysis_type = 'ocv'")
        else:
            self.verbose_append("set_db timing_analysis_cppr both")
            self.verbose_append("set_db timing_analysis_type ocv")

        # Read LEF layouts.
        lef_files = self.technology.read_libs([
            hammer_tech.filters.lef_filter
        ], hammer_tech.HammerTechnologyUtils.to_plain_item)
        if self.hierarchical_mode.is_nonleaf_hierarchical():
            ilm_lefs = list(map(lambda ilm: ilm.lef, self.get_input_ilms()))
            lef_files.extend(ilm_lefs)
        if self.use_python:
            lef_str = ', '.join(repr(f) for f in lef_files)
            self.py_append(f"read_physical(lef=[{lef_str}])")
        else:
            self.verbose_append("read_physical -lef {{ {files} }}".format(files=" ".join(lef_files)))

        # Read timing libraries.
        mmmc_path = os.path.join(self.run_dir, "mmmc.tcl")
        self.write_contents_to_path(self.generate_mmmc_script(), mmmc_path)
        if self.use_python:
            self.py_append(f"read_mmmc({mmmc_path!r})")
        else:
            self.verbose_append("read_mmmc {mmmc_path}".format(mmmc_path=mmmc_path))

        # Read netlist.
        if not self.check_input_files([".v", ".v.gz"]):
            return False
        abspath_input_files = list(map(lambda name: os.path.join(os.getcwd(), name), self.input_files))
        if self.use_python:
            if len(abspath_input_files) == 1:
                self.py_append(f"read_netlist({abspath_input_files[0]!r}, top={self.top_module!r})")
            else:
                files_str = ' '.join(abspath_input_files)
                self.py_append(f"read_netlist({files_str!r}, top={self.top_module!r})")
        else:
            self.verbose_append("read_netlist {{ {files} }} -top {top}".format(
                files=" ".join(abspath_input_files), top=self.top_module))

        if self.hierarchical_mode.is_nonleaf_hierarchical():
            for ilm in self.get_input_ilms():
                if self.use_python:
                    self.py_append(f"read_ilm(cell={ilm.module!r}, directory={ilm.dir!r})")
                else:
                    self.verbose_append("read_ilm -cell {module} -directory {dir}".format(dir=ilm.dir, module=ilm.module))

        # Emit init_power_nets and init_ground_nets in case CPF/UPF is not used
        spec_mode = self.get_setting("vlsi.inputs.power_spec_mode")  # type: str
        if spec_mode == "empty":
            power_supplies = self.get_independent_power_nets()  # type: List[Supply]
            power_nets = " ".join(map(lambda s: s.name, power_supplies))
            ground_supplies = self.get_independent_ground_nets()  # type: List[Supply]
            ground_nets = " ".join(map(lambda s: s.name, ground_supplies))
            if self.use_python:
                self.py_append(f"db().init_power_nets = {power_nets!r}")
                self.py_append(f"db().init_ground_nets = {ground_nets!r}")
            else:
                self.verbose_append("set_db init_power_nets {{{n}}}".format(n=power_nets))
                self.verbose_append("set_db init_ground_nets {{{n}}}".format(n=ground_nets))

        if self.use_python:
            self.py_append("init_design()")
        else:
            self.verbose_append("init_design")

        # Set the top and bottom global/detail routing layers.
        layers = self.get_setting("vlsi.technology.routing_layers")
        if layers is not None:
            if self.version() >= self.version_number("201"):
                if self.use_python:
                    self.py_append(f"db().design_bottom_routing_layer = {layers[0]!r}")
                    self.py_append(f"db().design_top_routing_layer = {layers[1]!r}")
                else:
                    self.verbose_append(f"set_db design_bottom_routing_layer {layers[0]}")
                    self.verbose_append(f"set_db design_top_routing_layer {layers[1]}")
            else:
                if self.use_python:
                    self.py_append(f"db().route_early_global_bottom_layer = {layers[0]!r}")
                    self.py_append(f"db().route_early_global_top_layer = {layers[1]!r}")
                    self.py_append(f"db().route_design_bottom_layer = {layers[0]!r}")
                    self.py_append(f"db().route_design_top_layer = {layers[1]!r}")
                else:
                    self.verbose_append(f"set_db route_early_global_bottom_layer {layers[0]}")
                    self.verbose_append(f"set_db route_early_global_top_layer {layers[1]}")
                    self.verbose_append(f"set_db route_design_bottom_layer {layers[0]}")
                    self.verbose_append(f"set_db route_design_top_layer {layers[1]}")

        # Set design effort.
        flow_effort = self.get_setting('par.innovus.design_flow_effort')
        power_effort = self.get_setting('par.innovus.design_power_effort')
        if self.use_python:
            self.py_append(f"db().design_flow_effort = {flow_effort!r}")
            self.py_append(f"db().design_power_effort = {power_effort!r}")
        else:
            self.verbose_append(f"set_db design_flow_effort {flow_effort}")
            self.verbose_append(f"set_db design_power_effort {power_effort}")

        return True

    def _generate_dont_use_commands_python(self) -> None:
        """Python-native equivalent of generate_dont_use_commands() for Innovus 25.1+."""
        for cell in self.get_dont_use_list():
            if not cell.startswith("*/"):
                cell = "*/" + cell
            # Strip the "*/" prefix for the pattern match against base_name
            pattern = cell[2:] if cell.startswith("*/") else cell
            self.py_append(f"# set_dont_use {cell}")
            self.py_append(f"_cells = db().lib_cells('{cell}')")
            self.py_append(f"if _cells:")
            self.py_append(f"    set_dont_use(_cells)")
            self.py_append(f"else:")
            self.py_append(f"    print('WARNING: cell {cell} was not found for set_dont_use')")
            self.py_append("")

    def _child_modules_python(self) -> None:
        """Python-native equivalent of child_modules_tcl() for Innovus 25.1+."""
        import json as _json
        child_modules_path = os.path.join(self.run_dir, "find_child_modules.json")

        if self.get_setting("vlsi.inputs.hierarchical.config_source") != "manual":
            self.logger.warning('''
            Hierarchical write_regs requires having vlsi.inputs.hierarchical.manual_modules specified.
            You may have problems with register forcing in gate-level sim.
            ''')
            self.py_append(f"# Write empty child modules")
            self.py_append(f"with open({repr(child_modules_path)}, 'w') as _f:")
            self.py_append(f"    _f.write('{{}}')")
        else:
            child_modules = list(next(
                d for i, d in enumerate(self.get_setting("vlsi.inputs.hierarchical.manual_modules"))
                if self.top_module in d
            ).values())[0]
            self.py_append(f"# Write child module instance paths")
            self.py_append(f"_child_modules = {{}}")
            for cell in child_modules:
                self.py_append(f"_inst_paths = get_db(get_db('modules', _if='.name=={cell}'), '.hinsts.name')")
                self.py_append(f"_child_modules['{cell}'] = _inst_paths if isinstance(_inst_paths, list) else _inst_paths.split()")
            self.py_append(f"with open({repr(child_modules_path)}, 'w') as _f:")
            self.py_append(f"    import json; json.dump(_child_modules, _f, indent=2)")
            self.py_append("")





    def place_bumps(self) -> bool:
        bumps = self.get_bumps()
        if bumps is not None:
            bump_array_width = Decimal(str((bumps.x - 1) * bumps.pitch_x))
            bump_array_height = Decimal(str((bumps.y - 1) * bumps.pitch_y))
            fp_consts = self.get_placement_constraints()
            fp_width = Decimal(0)
            fp_height = Decimal(0)
            for const in fp_consts:
                if const.type == PlacementConstraintType.TopLevel:
                    fp_width = const.width
                    fp_height = const.height
            if fp_width == 0 or fp_height == 0:
                raise ValueError("Floorplan does not specify a TopLevel constraint or it has no dimensions")
            bump_offset_x = (Decimal(str(fp_width)) - bump_array_width) / 2 + bumps.global_x_offset
            bump_offset_y = (Decimal(str(fp_height)) - bump_array_height) / 2 + bumps.global_y_offset
            power_ground_nets = list(map(lambda x: x.name, self.get_independent_power_nets() + self.get_independent_ground_nets()))
            block_layer = self.get_setting("vlsi.technology.bump_block_cut_layer")  # type: str
            for bump in bumps.assignments:
                bx = float(bump_offset_x + Decimal(str(bump.x - 1)) * Decimal(str(bumps.pitch_x)))
                by = float(bump_offset_y + Decimal(str(bump.y - 1)) * Decimal(str(bumps.pitch_y)))
                cell = bump.custom_cell if bump.custom_cell is not None else bumps.cell
                name_fmt = f"Bump_{bump.x}.{bump.y}"
                if self.use_python:
                    self.py_append(f"create_bump(allow_overlap_control='keep_all', cell={cell!r}, location_type='cell_center', name_format={name_fmt!r}, orient='r0', location=[{bx}, {by}])")
                else:
                    self.append(f'create_bump -allow_overlap_control keep_all -cell {cell} -location_type cell_center -name_format "Bump_{bump.x}.{bump.y}" -orient r0 -location "{bx} {by}"')
                if not bump.no_connect:
                    if bump.name in power_ground_nets:
                        if self.use_python:
                            self.py_append(f"select_bumps(bumps={name_fmt!r})")
                            self.py_append(f"assign_pg_bumps(selected=True, nets={bump.name!r})")
                            self.py_append("deselect_bumps()")
                        else:
                            self.append(f'select_bumps -bumps "Bump_{bump.x}.{bump.y}"')
                            self.append(f"assign_pg_bumps -selected -nets {bump.name}")
                            self.append("deselect_bumps")
                    else:
                        if self.use_python:
                            self.py_append(f"assign_signal_to_bump(bumps={name_fmt!r}, net={bump.name!r})")
                        else:
                            self.append(f'assign_signal_to_bump -bumps "Bump_{bump.x}.{bump.y}" -net {bump.name}')
                if self.use_python:
                    bname = f"Bump_{bump.x}.{bump.y}"
                    blk_name = f"Bump_{bump.x}_{bump.y}_blockage"
                    self.py_append(f"_bbox = db().bumps('{bname}').bbox")
                    self.py_append(f"create_route_blockage(name='{blk_name}', layers=[{block_layer!r}], rects=[[_bbox.ll.x, _bbox.ll.y, _bbox.ur.x, _bbox.ur.y]])")
                else:
                    layer_options = f"-layers {{{block_layer}}} -rects" if self.version() >= self.version_number("181") else f"-cut_layers {{{block_layer}}} -area"
                    self.append(f'create_route_blockage -name Bump_{bump.x}_{bump.y}_blockage {layer_options} "[get_db bump:Bump_{bump.x}.{bump.y} .bbox.ll.x] [get_db bump:Bump_{bump.x}.{bump.y} .bbox.ll.y] [get_db bump:Bump_{bump.x}.{bump.y} .bbox.ur.x] [get_db bump:Bump_{bump.x}.{bump.y} .bbox.ur.y]"')

            if self.version() >= self.version_number("231") and self.get_setting("par.innovus.early_route_bumps"):
                if self.use_python:
                    self.py_append("db().route_early_global_route_bump_nets = True")
                else:
                    self.append("set_db route_early_global_route_bump_nets true")
        return True

    def place_tap_cells(self) -> bool:
        tap_cells = self.technology.get_special_cell_by_type(CellType.TapCell)

        if len(tap_cells) == 0:
            self.logger.warning("Tap cells are improperly defined in the tech plugin and will not be added. This step should be overridden with a user hook or tapcell special cell should be added to the tech.json.")
            return True

        tap_cell = tap_cells[0].name[0]

        try:
            interval = self.get_setting("vlsi.technology.tap_cell_interval")
            offset = self.get_setting("vlsi.technology.tap_cell_offset")
            if self.use_python:
                self.py_append(f"db().add_well_taps_cell = {tap_cell!r}")
                self.py_append(f"add_well_taps(cell_interval={interval}, in_row_offset={offset})")
            else:
                self.append("set_db add_well_taps_cell {TAP_CELL}".format(TAP_CELL=tap_cell))
                self.append("add_well_taps -cell_interval {INTERVAL} -in_row_offset {OFFSET}".format(INTERVAL=interval, OFFSET=offset))
        except KeyError:
            pass
        return True

    def place_pins(self) -> bool:
        fp_consts = self.get_placement_constraints()
        topconst = None  # type: Optional[PlacementConstraint]
        for const in fp_consts:
            if const.type == PlacementConstraintType.TopLevel:
                topconst = const
        if topconst is None:
            self.logger.fatal("Cannot find top-level constraints to place pins")
            return False

        power_pin_layers = self.get_setting("par.generate_power_straps_options.by_tracks.pin_layers")

        const = cast(PlacementConstraint, topconst)
        assert isinstance(const.margins, Margins), "Margins must be defined for the top level"
        fp_llx = const.margins.left
        fp_lly = const.margins.bottom
        fp_urx = const.width - const.margins.right
        fp_ury = const.height - const.margins.top

        pin_assignments = self.get_pin_assignments()
        if self.use_python:
            self.py_append("db().assign_pins_edit_in_batch = True")
            self.py_append(f"db().assign_pins_promoted_macro_bottom_layer = {self.get_stackup().get_metal_by_index(1).name!r}")
            self.py_append(f"db().assign_pins_promoted_macro_top_layer = {self.get_stackup().get_metal_by_index(-1).name!r}")
            self.py_append("_all_ppins = []")
        else:
            self.verbose_append("set_db assign_pins_edit_in_batch true")
            self.append(f"set_db assign_pins_promoted_macro_bottom_layer {self.get_stackup().get_metal_by_index(1).name}")
            self.append(f"set_db assign_pins_promoted_macro_top_layer {self.get_stackup().get_metal_by_index(-1).name}")
            self.append('set all_ppins ""')

        promoted_pins = []  # type: List[str]
        for pin in pin_assignments:
            if pin.preplaced:
                if self.use_python:
                    self.py_append(f"_ppins = db().nets('{pin.pins}').driver_pins or db().nets('{pin.pins}').load_pins")
                    self.py_append("_all_ppins.extend(_ppins)")
                    self.py_append(f"set_promoted_macro_pin(insts=[p.inst.name for p in _ppins], pins=[p.base_name for p in _ppins])")
                    self.py_append(f"set_dont_touch([p.net for p in _ppins])")
                    self.py_append(f"[setattr(p.net, 'skip_routing', True) for p in _ppins]")
                else:
                    self.append(f'set ppins [get_db [get_nets {pin.pins}] .driver_pins]')
                    self.append(f'if {{$ppins eq ""}} {{set ppins [get_db [get_nets {pin.pins}] .load_pins]}}')
                    self.append("lappend all_ppins $ppins")
                    self.append("set_promoted_macro_pin -insts [get_db $ppins .inst.name] -pins [get_db $ppins .base_name]")
                    self.append("set_dont_touch [get_db $ppins .net]")
                    self.append("set_db [get_db $ppins .net] .skip_routing true")
            else:
                # TODO: Do we need pin blockages for our layers?
                # Seems like we will only need special pin blockages if the vias are larger than the straps

                cadence_side = None  # type: Optional[str]
                if pin.side is not None:
                    if pin.side == "internal":
                        cadence_side = "inside"
                    else:
                        cadence_side = pin.side
                side_arg = get_or_else(optional_map(cadence_side, lambda s: "-side " + s), "")

                start_arg = ""
                end_arg = ""
                assign_arg = ""
                pattern_arg = ""

                if pin.location is None:
                    start_arg = "-{start} {{ {sx} {sy} }}".format(
                        start="start" if pin.side == "bottom" or pin.side == "right" else "end",
                        sx=fp_urx if pin.side != "left" else fp_llx,
                        sy=fp_ury if pin.side != "bottom" else fp_lly)

                    end_arg = "-{end} {{ {ex} {ey} }}".format(
                        end="end" if pin.side == "bottom" or pin.side == "right" else "start",
                        ex=fp_llx if pin.side != "right" else fp_urx,
                        ey=fp_lly if pin.side != "top" else fp_ury
                    )
                    if pin.layers and len(pin.layers) > 1:
                        pattern_arg = "-pattern fill_optimised"
                    else:
                        pattern_arg = "-spread_type range"
                else:
                    assign_arg = "-assign {{ {x} {y} }}".format(x=pin.location[0], y=pin.location[1])

                layers_arg = ""

                if set(pin.layers or []).intersection(set(power_pin_layers)):
                    self.logger.error("Signal pins will be generated on the same layer(s) as power pins. Double-check to see if intended.")

                if pin.layers is not None and len(pin.layers) > 0:
                    layers_arg = "-layer {{ {} }}".format(" ".join(pin.layers))

                width_arg = get_or_else(optional_map(pin.width, lambda f: "-pin_width {f}".format(f=f)), "")
                depth_arg = get_or_else(optional_map(pin.depth, lambda f: "-pin_depth {f}".format(f=f)), "")

                cmd = [
                    "edit_pin",
                    "-fixed_pin",
                    "-pin", pin.pins,
                    "-hinst", self.top_module,
                    pattern_arg,
                    layers_arg,
                    side_arg,
                    start_arg,
                    end_arg,
                    assign_arg,
                    width_arg,
                    depth_arg
                ]

                if self.use_python:
                    py_kwargs = {}
                    py_kwargs['fixed_pin'] = 'True'
                    py_kwargs['pin'] = repr(pin.pins)
                    py_kwargs['hinst'] = repr(self.top_module)
                    if cadence_side:
                        py_kwargs['side'] = repr(cadence_side)
                    if pin.layers:
                        py_kwargs['layer'] = repr(pin.layers) if len(pin.layers) > 1 else repr(pin.layers[0])
                    if pin.location is not None:
                        py_kwargs['assign'] = f"[{pin.location[0]}, {pin.location[1]}]"
                    else:
                        sx = float(fp_urx if pin.side != "left" else fp_llx)
                        sy = float(fp_ury if pin.side != "bottom" else fp_lly)
                        ex = float(fp_llx if pin.side != "right" else fp_urx)
                        ey = float(fp_lly if pin.side != "top" else fp_ury)
                        start_key = "start" if pin.side in ("bottom", "right") else "end"
                        end_key = "end" if pin.side in ("bottom", "right") else "start"
                        py_kwargs[start_key] = f"[{sx}, {sy}]"
                        py_kwargs[end_key] = f"[{ex}, {ey}]"
                        if pin.layers and len(pin.layers) > 1:
                            py_kwargs['pattern'] = "'fill_optimised'"
                        else:
                            py_kwargs['spread_type'] = "'range'"
                    if pin.width is not None:
                        py_kwargs['pin_width'] = pin.width
                    if pin.depth is not None:
                        py_kwargs['pin_depth'] = pin.depth
                    kw_str = ', '.join(f"{k}={v}" for k, v in py_kwargs.items())
                    self.py_append(f"edit_pin({kw_str})")
                else:
                    self.verbose_append(" ".join(cmd))

        if self.use_python:
            self.py_append("if _all_ppins: assign_io_pins(move_fixed_pin=True, pins=[p.net.name for p in _all_ppins])")
            self.py_append("db().assign_pins_edit_in_batch = False")
        else:
            self.verbose_append("if {[llength $all_ppins] ne 0} {assign_io_pins -move_fixed_pin -pins [get_db $all_ppins .net.name]}")
            self.verbose_append("set_db assign_pins_edit_in_batch false")
        return True

    def power_straps(self) -> bool:
        """Place the power straps for the design."""
        power_straps_tcl = os.path.join(self.run_dir, "power_straps.tcl")
        self.write_contents_to_path("\n".join(self.create_power_straps_tcl()), power_straps_tcl)
        if self.use_python:
            self.py_append(f"source('{power_straps_tcl}', echo=True, verbose=True)")
        else:
            self.verbose_append("source -echo -verbose {}".format(power_straps_tcl))
        return True

    def place_opt_design(self) -> bool:
        """Place the design and do pre-routing optimization."""
        if self.use_python:
            self.py_append("_unplaced = db().ports().filter(lambda x: x.place_status == 'unplaced')")
            self.py_append("if _unplaced:")
            self.py_append("    raise RuntimeError(f'Some pins remain unplaced: {[p.name for p in _unplaced]}')")
        else:
            self.append('''
            set unplaced_pins [get_db ports -if {.place_status == unplaced}]
            if {$unplaced_pins ne ""} {
                print_message -error "Some pins remain unplaced, which will cause invalid placement and routing. These are the unplaced pins: $unplaced_pins"
                exit 2
            }
            ''', clean=True)

        if self.hierarchical_mode.is_nonleaf_hierarchical():
            if self.use_python:
                self.py_append("flatten_ilm()")
                self.py_append(f"update_constraint_mode(name={self.constraint_mode!r}, ilm_sdc_files={self.post_synth_sdc!r})")
            else:
                self.verbose_append('''
            flatten_ilm
            update_constraint_mode -name {name} -ilm_sdc_files {sdc}
            '''.format(name=self.constraint_mode, sdc=self.post_synth_sdc), clean=True)

        if self.version() >= self.version_number("221") and self.version() < self.version_number("231"):
            if self.use_python:
                self.py_append("db().opt_enable_podv2_clock_opt_flow = True")
            else:
                self.verbose_append("set_db opt_enable_podv2_clock_opt_flow true")

        if self.use_python:
            self.py_append("place_opt_design()")
        else:
            self.verbose_append("place_opt_design")
        return True

    def clock_tree(self) -> bool:
        """Setup and route a clock tree for clock nets."""
        if len(self.get_clock_ports()) > 0 or len(self.get_setting("vlsi.inputs.custom_sdc_files")) > 0:
            if self.use_python:
                self.py_append("db().opt_fix_fanout_load = True")
            else:
                self.verbose_append("set_db opt_fix_fanout_load true")

            buffer_cells = self.technology.get_special_cell_by_type(CellType.CTSBuffer)
            if len(buffer_cells) > 0:
                if self.use_python:
                    self.py_append(f"db().cts_buffer_cells = {buffer_cells[0].name!r}")
                else:
                    self.append(f"set_db cts_buffer_cells {{{' '.join(buffer_cells[0].name)}}}")
            inverter_cells = self.technology.get_special_cell_by_type(CellType.CTSInverter)
            if len(inverter_cells) > 0:
                if self.use_python:
                    self.py_append(f"db().cts_inverter_cells = {inverter_cells[0].name!r}")
                else:
                    self.append(f"set_db cts_inverter_cells {{{' '.join(inverter_cells[0].name)}}}")
            gate_cells = self.technology.get_special_cell_by_type(CellType.CTSGate)
            if len(gate_cells) > 0:
                if self.use_python:
                    self.py_append(f"db().cts_clock_gating_cells = {gate_cells[0].name!r}")
                else:
                    self.append(f"set_db cts_clock_gating_cells {{{' '.join(gate_cells[0].name)}}}")
            logic_cells = self.technology.get_special_cell_by_type(CellType.CTSLogic)
            if len(logic_cells) > 0:
                if self.use_python:
                    self.py_append(f"db().cts_logic_cells = {logic_cells[0].name!r}")
                else:
                    self.append(f"set_db cts_logic_cells {{{' '.join(logic_cells[0].name)}}}")

            if self.use_python:
                self.py_append("create_clock_tree_spec()")
            else:
                self.verbose_append("create_clock_tree_spec")

            if self.version() >= self.version_number("221"):
                if bool(self.get_setting("par.innovus.use_cco")):
                    if self.use_python:
                        self.py_append("clock_opt_design(hold=True, timing_debug_report=True)")
                    else:
                        self.verbose_append("clock_opt_design -hold -timing_debug_report")
                else:
                    if self.use_python:
                        self.py_append("clock_opt_design(cts=True, timing_debug_report=True)")
                    else:
                        self.verbose_append("clock_opt_design -cts -timing_debug_report")
            else:
                if bool(self.get_setting("par.innovus.use_cco")):
                    if self.use_python:
                        self.py_append("ccopt_design(hold=True, timing_debug_report=True)")
                    else:
                        self.verbose_append("ccopt_design -hold -timing_debug_report")
                else:
                    if self.use_python:
                        self.py_append("clock_design()")
                    else:
                        self.verbose_append("clock_design")

        if self.hierarchical_mode.is_nonleaf_hierarchical():
            if self.use_python:
                self.py_append("unflatten_ilm()")
            else:
                self.verbose_append("unflatten_ilm")
        return True

    def add_fillers(self) -> bool:
        """add decap and filler cells"""
        decaps = self.technology.get_special_cell_by_type(CellType.Decap)
        stdfillers = self.technology.get_special_cell_by_type(CellType.StdFiller)

        if len(decaps) == 0:
            self.logger.info("The technology plugin 'special cells: decap' field does not exist. It should specify a list of decap cells. Filling with stdfiller instead.")
        else:
            decap_cells = decaps[0].name
            decap_caps = []  # type: List[float]
            cap_unit = self.get_cap_unit().value_prefix + self.get_cap_unit().unit
            if decaps[0].size is not None:
                decap_caps = list(map(lambda x: CapacitanceValue(x).value_in_units(cap_unit), decaps[0].size))
            if len(decap_cells) != len(decap_caps):
                self.logger.error("Each decap cell in the name list must have a corresponding decapacitance value in the size list.")
            decap_consts = list(filter(lambda x: x.target=="capacitance", self.get_decap_constraints()))
            if len(decap_consts) > 0:
                if decap_caps is None:
                    self.logger.warning("No decap capacitances specified but decap constraints with target: 'capacitance' exist. Add decap capacitances to the tech plugin!")
                else:
                    for (cell, cap) in zip(decap_cells, decap_caps):
                        if self.use_python:
                            self.py_append(f"add_decap_cell_candidates({cell!r}, {cap})")
                        else:
                            self.append("add_decap_cell_candidates {CELL} {CAP}".format(CELL=cell, CAP=cap))
                    for const in decap_consts:
                        assert isinstance(const.capacitance, CapacitanceValue)
                        cap_val = const.capacitance.value_in_units(cap_unit)
                        if all(c is not None for c in (const.x, const.y, const.width, const.height)):
                            assert isinstance(const.x, Decimal)
                            assert isinstance(const.y, Decimal)
                            assert isinstance(const.width, Decimal)
                            assert isinstance(const.height, Decimal)
                            if self.use_python:
                                self.py_append(f"add_decaps(effort='high', total_cap={cap_val}, area=[{const.x}, {const.y}, {const.x+const.width}, {const.y+const.height}])")
                            else:
                                area_str = " ".join(("-area", str(const.x), str(const.y), str(const.x+const.width), str(const.y+const.height)))
                                self.verbose_append(f"add_decaps -effort high -total_cap {cap_val} {area_str}")
                        else:
                            if self.use_python:
                                self.py_append(f"add_decaps(effort='high', total_cap={cap_val})")
                            else:
                                self.verbose_append(f"add_decaps -effort high -total_cap {cap_val}")

        if len(stdfillers) == 0:
            self.logger.warning(
                "The technology plugin 'special cells: stdfiller' field does not exist. It should specify a list of (non IO) filler cells. No filler will be added. You can override this with an add_fillers hook if you do not specify filler cells in the technology plugin.")
        else:
            if len(decaps) > 0:
                fill_cells = list(map(lambda c: str(c), decaps[0].name))
                if self.use_python:
                    self.py_append(f"db().add_fillers_cells = {' '.join(fill_cells)!r}")
                else:
                    self.append("set_db add_fillers_cells \"{FILLER}\"".format(FILLER=" ".join(fill_cells)))
                decap_consts = list(filter(lambda x: x.target=="density", self.get_decap_constraints()))
                for const in decap_consts:
                    if all(c is not None for c in (const.x, const.y, const.width, const.height)):
                        assert isinstance(const.x, Decimal)
                        assert isinstance(const.y, Decimal)
                        assert isinstance(const.width, Decimal)
                        assert isinstance(const.height, Decimal)
                        if self.use_python:
                            self.py_append(f"add_fillers(density={const.density}, area=[{const.x}, {const.y}, {const.x+const.width}, {const.y+const.height}])")
                        else:
                            area_str = " ".join(("-area", str(const.x), str(const.y), str(const.x+const.width), str(const.y+const.height)))
                            self.verbose_append(f"add_fillers -density {const.density} {area_str}")
                    else:
                        # a density target without an area applies to the whole core
                        if self.use_python:
                            self.py_append(f"add_fillers(density={const.density})")
                        else:
                            self.verbose_append(f"add_fillers -density {const.density}")
                if len(self.get_decap_constraints()) == 0:
                    if self.use_python:
                        self.py_append("add_fillers()")
                    else:
                        self.verbose_append("add_fillers")

            fill_cells = list(map(lambda c: str(c), stdfillers[0].name))
            if self.use_python:
                self.py_append(f"db().add_fillers_cells = {' '.join(fill_cells)!r}")
                self.py_append("add_fillers()")
            else:
                self.append("set_db add_fillers_cells \"{FILLER}\"".format(FILLER=" ".join(fill_cells)))
                self.verbose_append("add_fillers")
        return True


    def route_design(self) -> bool:
        """Route the design."""
        if self.hierarchical_mode.is_nonleaf_hierarchical():
            if self.use_python:
                self.py_append("flatten_ilm()")
            else:
                self.verbose_append("flatten_ilm")

        if self.use_python:
            self.py_append("db().design_express_route = True")
            self.py_append("route_design()")
        else:
            self.verbose_append("set_db design_express_route true")
            self.verbose_append("route_design")
        return True

    def opt_settings(self) -> str:
        """Returns TCL opt settings string (used for TCL mode). For Python mode use emit_opt_settings_python()."""
        cmds = []
        cmds.append("set_db opt_post_route_hold_recovery auto")
        cmds.append("set_db opt_post_route_fix_si_transitions true")
        cmds.append("set_db opt_verbose true")
        cmds.append("set_db opt_detail_drv_failure_reason true")
        if self.version() >= self.version_number("221"):
            cmds.append("set_db opt_sequential_genus_restructure_report_failure_reason true")
        return "\n".join(cmds)

    def _emit_opt_settings_python(self) -> None:
        """Emit Python-native opt settings."""
        self.py_append("db().opt_post_route_hold_recovery = 'auto'")
        self.py_append("db().opt_post_route_fix_si_transitions = True")
        self.py_append("db().opt_verbose = True")
        self.py_append("db().opt_detail_drv_failure_reason = True")
        if self.version() >= self.version_number("221"):
            self.py_append("db().opt_sequential_genus_restructure_report_failure_reason = True")

    def opt_design(self) -> bool:
        """
        Post-route optimization and fix setup & hold time violations.
        -expanded_views creates timing reports for each MMMC view.
        """
        if self.use_python:
            self._emit_opt_settings_python()
            self.py_append("opt_design(post_route=True, setup=True, hold=True, expanded_views=True, timing_debug_report=True)")
            if self.hierarchical_mode.is_nonleaf_hierarchical():
                self.py_append("unflatten_ilm()")
        else:
            self.append(self.opt_settings())
            self.verbose_append("opt_design -post_route -setup -hold -expanded_views -timing_debug_report")
            if self.hierarchical_mode.is_nonleaf_hierarchical():
                self.verbose_append("unflatten_ilm")
        return True

    def route_opt_design(self) -> bool:
        """
        Routing and post-route optimization. New flow as default in 23.1.
        Performs more optimization during the routing stage.
        Fixes setup, hold, and DRV violations.
        """
        if self.hierarchical_mode.is_nonleaf_hierarchical():
            if self.use_python:
                self.py_append("flatten_ilm()")
            else:
                self.verbose_append("flatten_ilm")

        if self.use_python:
            self.py_append("db().design_express_route = True")
            self._emit_opt_settings_python()
        else:
            self.verbose_append("set_db design_express_route true")
            self.append(self.opt_settings())

        # For Innovus 25.1+ without QRC tech files, use separate route + opt flow
        if self.version() >= self.version_number("251"):
            qrc_tech = self.get_qrc_tech() if hasattr(self, 'get_qrc_tech') else ''
            if qrc_tech == '':
                if self.use_python:
                    self.py_append("db().extract_rc_engine = 'pre_route'")
                    self.py_append("route_design()")
                    self.py_append("db().extract_rc_engine = 'post_route'")
                    self.py_append("opt_design(post_route=True, timing_debug_report=True)")
                    if self.hierarchical_mode.is_nonleaf_hierarchical():
                        self.py_append("unflatten_ilm()")
                else:
                    self.verbose_append("set_db extract_rc_engine pre_route")
                    self.verbose_append("route_design")
                    self.verbose_append("set_db extract_rc_engine post_route")
                    self.verbose_append("opt_design -post_route -timing_debug_report")
                    if self.hierarchical_mode.is_nonleaf_hierarchical():
                        self.verbose_append("unflatten_ilm")
                return True

        if self.use_python:
            self.py_append("route_opt_design(timing_debug_report=True)")
            if self.hierarchical_mode.is_nonleaf_hierarchical():
                self.py_append("unflatten_ilm()")
        else:
            self.verbose_append("route_opt_design -timing_debug_report")
            if self.hierarchical_mode.is_nonleaf_hierarchical():
                self.verbose_append("unflatten_ilm")
        return True

    def opt_signoff(self) -> bool:
        """
        Signoff timing and optimization.
        Should only be run after all implementation flows to obtain better timing,
        setup/hold fixing, DRV fixing, and power optimization.
        Note: runs Tempus in batch mode.
        """
        if self.hierarchical_mode.is_nonleaf_hierarchical():
            if self.use_python:
                self.py_append("flatten_ilm()")
            else:
                self.verbose_append("flatten_ilm")

        cell_types = [CellType.CTSBuffer, CellType.CTSInverter, CellType.CTSGate, CellType.CTSLogic]
        cells_by_type = chain(*map(lambda c: self.technology.get_special_cell_by_type(c), cell_types))
        flat_cells = chain(*map(lambda cells: cells.name, cells_by_type))
        regexp = "|".join(flat_cells)
        max_threads = self.get_setting('vlsi.core.max_threads')

        if self.use_python:
            self.py_append(f"db().opt_signoff_clock_cell_list = db().lib_cells().filter(lambda x: __import__('re').search({regexp!r}, x.base_name)).base_name")
            self.py_append("db().opt_signoff_fix_data_drv = True")
            self.py_append("db().opt_signoff_fix_clock_drv = True")
            self.py_append(f"set_multi_cpu_usage(remote_host=1, cpu_per_remote_host={max_threads})")
            self.py_append("time_design_signoff()")
            self.py_append("opt_signoff(_all=True)")
            if self.hierarchical_mode.is_nonleaf_hierarchical():
                self.py_append("unflatten_ilm()")
        else:
            self.verbose_append(f"set_db opt_signoff_clock_cell_list [lsearch -regexp -all -inline [get_db lib_cells .base_name] {regexp}]")
            self.verbose_append("set_db opt_signoff_fix_data_drv true")
            self.verbose_append("set_db opt_signoff_fix_clock_drv true")
            self.verbose_append(f"set_multi_cpu_usage -remote_host 1 -cpu_per_remote_host {max_threads}")
            self.verbose_append("time_design_signoff")
            self.verbose_append("opt_signoff -all")
            if self.hierarchical_mode.is_nonleaf_hierarchical():
                self.verbose_append("unflatten_ilm")
        return True

    def assemble_design(self) -> bool:
        # TODO: implement the assemble_design step.
        return True

    def write_netlist(self) -> bool:
        pcells = self.get_physical_only_cells()
        if self.use_python:
            self.py_append("db().write_stream_virtual_connection = False")
            for pwr_gnd_net in (self.get_all_power_nets() + self.get_all_ground_nets()):
                if pwr_gnd_net.tie is not None:
                    self.py_append(f"connect_global_net({pwr_gnd_net.tie!r}, type='net', net_base_name={pwr_gnd_net.name!r})")
            self.py_append(f"write_netlist({self.output_netlist_filename!r}, top_module_first=True, top_module={self.top_module!r}, exclude_leaf_cells=True, phys=True, flat=True, exclude_insts_of_cells={pcells!r})")
            self.py_append(f"write_netlist({self.output_physical_netlist_filename!r}, top_module_first=True, top_module={self.top_module!r}, exclude_leaf_cells=True, phys=True, exclude_insts_of_cells={pcells!r})")
            self.py_append(f"write_netlist({self.output_sim_netlist_filename!r}, top_module_first=True, top_module={self.top_module!r}, exclude_leaf_cells=True, exclude_insts_of_cells={pcells!r})")
        else:
            self.verbose_append("set_db write_stream_virtual_connection false")
            for pwr_gnd_net in (self.get_all_power_nets() + self.get_all_ground_nets()):
                if pwr_gnd_net.tie is not None:
                    self.verbose_append("connect_global_net {tie} -type net -net_base_name {net}".format(tie=pwr_gnd_net.tie, net=pwr_gnd_net.name))
            self.verbose_append("write_netlist {netlist} -top_module_first -top_module {top} -exclude_leaf_cells -phys -flat -exclude_insts_of_cells {{ {pcells} }} ".format(
                netlist=self.output_netlist_filename, top=self.top_module, pcells=" ".join(pcells)))
            self.verbose_append("write_netlist {netlist} -top_module_first -top_module {top} -exclude_leaf_cells -phys -exclude_insts_of_cells {{ {pcells} }} ".format(
                netlist=self.output_physical_netlist_filename, top=self.top_module, pcells=" ".join(pcells)))
            self.verbose_append("write_netlist {netlist} -top_module_first -top_module {top} -exclude_leaf_cells -exclude_insts_of_cells {{ {pcells} }} ".format(
                netlist=self.output_sim_netlist_filename, top=self.top_module, pcells=" ".join(pcells)))
        return True

    def write_gds(self) -> bool:
        map_file = get_or_else(
            optional_map(self.get_gds_map_file(), lambda f: "-map_file {}".format(f)),
            ""
        )

        gds_files = self.technology.read_libs([
            hammer_tech.filters.gds_filter
        ], hammer_tech.HammerTechnologyUtils.to_plain_item)
        if self.hierarchical_mode.is_nonleaf_hierarchical():
            ilm_gds = list(map(lambda ilm: ilm.gds, self.get_input_ilms()))
            gds_files.extend(ilm_gds)

        # If we are not merging, then we want to use -output_macros.
        # output_macros means that Innovus should take any macros it has and
        # just output the cells into the GDS. These cells will not have physical
        # information inside them and will need to be merged with some other
        # step later. We do not care about uniquifying them because Innovus will
        # output a single cell for each instance (essentially already unique).

        # On the other hand, if we tell Innovus to do the merge then it is going
        # to get a GDS with potentially multiple child cells and we then tell it
        # to uniquify these child cells in case of name collisons. Without that
        # we could have one child that applies to all cells of that name which
        # is often not what you want.
        # For example, if macro ADC1 has a subcell Comparator which is different
        # from ADC2's subcell Comparator, we don't want ADC1's Comparator to
        # replace ADC2's Comparator.
        # Note that cells not present in the GDSes to be merged will be emitted
        # as-is in the output (like with -output_macros).
        merge_options = "-output_macros" if not self.get_setting("par.inputs.gds_merge") else "-uniquify_cell_names -merge {{ {} }}".format(
            " ".join(gds_files)
        )

        # If the user has specified the par.inputs.gds_precision_mode as
        # "auto", we make write_stream modify the GDS precision according to
        # the value of par.inputs.gds_precision. If this setting is not
        # specified then we fall back to the default behavior, which is to use
        # the units specified in the LEF. See documentation for write_stream
        # for what this switch does and what valid values are.
        unit = ""
        if (self.get_setting("par.inputs.gds_precision_mode") == "manual"):
            gds_precision = self.get_setting("par.inputs.gds_precision") or ""
            valid_values = [100, 200, 1000, 2000, 10000, 20000]
            if gds_precision in valid_values:
                unit = "-unit %s" % gds_precision
            else:
                self.logger.error(
                    "par.inputs.gds_precision value of \"%s\" must be one of %s" %(
                        gds_precision, ', '.join([str(x) for x in valid_values])));
                return False
        # "auto", i.e. not "manual", means not specifying anything extra.

        if self.use_python:
            kwargs = f"mode='ALL', format='stream'"
            gds_map = self.get_gds_map_file()
            if gds_map:
                kwargs += f", map_file={gds_map!r}"
            if unit:
                gds_precision = self.get_setting("par.inputs.gds_precision")
                kwargs += f", unit={gds_precision}"
            if self.get_setting("par.inputs.gds_merge"):
                kwargs += f", uniquify_cell_names=True, merge={gds_files!r}"
            else:
                kwargs += ", output_macros=True"
            self.py_append(f"write_stream({self.output_gds_filename!r}, {kwargs})")
        else:
            self.verbose_append(
                "write_stream -mode ALL -format stream {map_file} {merge_options} {unit} {gds}".format(
                map_file=map_file,
                merge_options=merge_options,
                gds=self.output_gds_filename,
                unit=unit
            ))
        return True

    def write_def(self) -> bool:
        if self.use_python:
            self.py_append(f"write_def({self.output_def_path!r}, floorplan=True, netlist=True, routing=True)")
        else:
            self.verbose_append("write_def {def_file} -floorplan -netlist -routing".format(def_file=self.output_def_path))
        return True

    def write_sdf(self) -> bool:
        if self.hierarchical_mode.is_nonleaf_hierarchical():
            if self.use_python:
                self.py_append("flatten_ilm()")
            else:
                self.verbose_append("flatten_ilm")

        corners = self.get_mmmc_corners()
        sdf_path = self.output_sdf_path
        if corners:
            max_view = next((c for c in corners if c.type is MMMCCornerType.Setup), None)
            min_view = next((c for c in corners if c.type is MMMCCornerType.Hold), None)
            typ_view = next((c for c in corners if c.type is MMMCCornerType.Extra), None)
            if self.use_python:
                kwargs = repr(sdf_path)
                if max_view: kwargs += f", max_view='{max_view.name}.setup_view'"
                if min_view: kwargs += f", min_view='{min_view.name}.hold_view'"
                if typ_view: kwargs += f", typical_view='{typ_view.name}.extra_view'"
                self.py_append(f"write_sdf({kwargs})")
            else:
                max_flag = f"-max_view {max_view.name}.setup_view" if max_view else ""
                min_flag = f"-min_view {min_view.name}.hold_view" if min_view else ""
                typ_flag = f"-typical_view {typ_view.name}.extra_view" if typ_view else ""
                self.verbose_append(f"write_sdf {max_flag} {min_flag} {typ_flag} {sdf_path}")
        else:
            if self.use_python:
                self.py_append(f"write_sdf({sdf_path!r})")
            else:
                self.verbose_append(f"write_sdf {sdf_path}")
        return True

    def write_spefs(self) -> bool:
        if self.use_python:
            self.py_append("db().extract_rc_coupled = True")
            self.py_append("extract_rc()")
        else:
            self.verbose_append("set_db extract_rc_coupled true")
            self.verbose_append("extract_rc")

        corners = self.get_mmmc_corners()
        if corners:
            for corner in corners:
                if corner.type is MMMCCornerType.Setup:
                    corner_type_name = "setup"
                elif corner.type is MMMCCornerType.Hold:
                    corner_type_name = "hold"
                elif corner.type is MMMCCornerType.Extra:
                    corner_type_name = "extra"
                else:
                    raise ValueError("Unsupported MMMCCornerType")
                spef_path = os.path.join(self.run_dir, f"{self.top_module}.{corner.name}.par.spef")
                rc_corner = f"{corner.name}.{corner_type_name}_rc"
                if self.use_python:
                    self.py_append(f"write_parasitics(spef_file={spef_path!r}, rc_corner={rc_corner!r})")
                else:
                    self.verbose_append(f"write_parasitics -spef_file {spef_path} -rc_corner {rc_corner}")
        else:
            spef_path = os.path.join(self.run_dir, f"{self.top_module}.par.spef")
            if self.use_python:
                self.py_append(f"write_parasitics(spef_file={spef_path!r})")
            else:
                self.verbose_append(f"write_parasitics -spef_file {spef_path}")
        return True



    @property
    def output_innovus_lib_name(self) -> str:
        return "{top}_FINAL".format(top=self.top_module)

    @property
    def generated_scripts_dir(self) -> str:
        return os.path.join(self.run_dir, "generated-scripts")

    @property
    def open_chip_script(self) -> str:
        return os.path.join(self.generated_scripts_dir, "open_chip")

    @property
    def open_chip_tcl(self) -> str:
        return self.open_chip_script + ".tcl"

    def write_regs(self) -> bool:
        """write regs info to be read in for simulation register forcing"""
        if self.hierarchical_mode.is_nonleaf_hierarchical():
            self.append('flatten_ilm')
            if self.use_python:
                self._child_modules_python()
            else:
                self.append(self.child_modules_tcl())

        if self.use_python:
            self._write_regs_python()
        else:
            self.append(self.write_regs_tcl())

        if self.hierarchical_mode.is_nonleaf_hierarchical():
            self.append('unflatten_ilm')
        self.ran_write_regs = True
        return True

    def _write_regs_python(self) -> None:
        """Python-native equivalent of write_regs_tcl() for Innovus 25.1+."""
        cells_path = self.all_cells_path
        regs_path  = self.all_regs_path

        # Write sequential cell names to find_regs_cells.json
        self.py_append("# Write sequential cell names")
        self.py_append("import json as _json")
        self.py_append("_refs = db().lib_cells().filter(lambda x: x.is_sequential).base_name")
        self.py_append(
            f"with open({repr(cells_path)}, 'w') as _f:\n"
            f"    _json.dump(list(_refs), _f, indent=4)"
        )
        self.py_append("")

        # Write register output pin paths to find_regs_paths.json
        self.py_append("# Write register output pin paths")
        self.py_append("_reg_insts = all_registers(edge_triggered=True, output_pins=True).to_db_list()")
        self.py_append("_regs = [x.name for x in _reg_insts if x.direction == 'out']")
        self.py_append(
            f"with open({repr(regs_path)}, 'w') as _f:\n"
            f"    _json.dump(_regs, _f, indent=4)"
        )
        self.py_append("")



    def write_design(self) -> bool:
        # Save the Innovus design.
        if self.use_python:
            self.py_append(f"write_db({self.output_innovus_lib_name!r}, _option_def=True, verilog=True)")
        else:
            self.verbose_append("write_db {lib_name} -def -verilog".format(lib_name=self.output_innovus_lib_name))

        # Write netlist
        self.write_netlist()

        # Write DEF
        self.write_def()

        # GDS streamout.
        self.write_gds()

        # Write SDF
        self.write_sdf()

        # Write SPEF
        self.write_spefs()

        # Make sure that generated-scripts exists.
        os.makedirs(self.generated_scripts_dir, exist_ok=True)

        self.ran_write_design=True

        return True

    @property
    def ran_write_regs(self) -> bool:
        """The write_regs step sets this to True if it was run."""
        return self.attr_getter("_ran_write_regs", False)

    @ran_write_regs.setter
    def ran_write_regs(self, val: bool) -> None:
        self.attr_setter("_ran_write_regs", val)

    @property
    def ran_write_design(self) -> bool:
        """The write_design step sets this to True if it was run."""
        return self.attr_getter("_ran_write_design", False)

    @ran_write_design.setter
    def ran_write_design(self, val: bool) -> None:
        self.attr_setter("_ran_write_design", val)

    @property
    def ran_write_ilm(self) -> bool:
        """The write_ilm stage sets this to True if it was run."""
        return self.attr_getter("_ran_write_ilm", False)

    @ran_write_ilm.setter
    def ran_write_ilm(self, val: bool) -> None:
        self.attr_setter("_ran_write_ilm", val)

    @property
    def ilm_dir_name(self) -> str:
        return os.path.join(self.run_dir, "{top}ILMDir".format(top=self.top_module))

    def write_ilm(self) -> bool:
        """Run time_design and write out the ILM."""
        if self.use_python:
            self.py_append("time_design(post_route=True)")
            self.py_append("time_design(post_route=True, hold=True)")
            # No Python binding exists for this one -- it is Tcl-only.
            self.py_append("tcl.eval('check_process_antenna')")
        else:
            self.verbose_append("time_design -post_route")
            self.verbose_append("time_design -post_route -hold")
            self.verbose_append("check_process_antenna")

        assert self.get_setting("par.generate_power_straps_method") == "by_tracks", "Hierarchical write_ilm currently requires auto power_straps by_tracks"
        top_layer = self.get_setting("par.generate_power_straps_options.by_tracks.pin_layers")
        assert len(top_layer) == 1, "Hierarchical write_ilm requires 1 pin layer specified"
        tl = top_layer[0]
        ilm_lef = f"{self.top_module}ILM.lef"
        if self.use_python:
            # Both take arguments the Python API cannot express: the LEF
            # version is a flag and the LEF name is positional, so keep the
            # Tcl forms verbatim.
            self.py_append(f"tcl.eval('write_lef_abstract -5.8 -top_layer {tl} -stripe_pins -pg_pin_layers {{{tl}}} {ilm_lef}')")
            self.py_append(f"tcl.eval('write_ilm -model_type all -to_dir {self.ilm_dir_name} -type_flex_ilm ilm')")
        else:
            self.verbose_append(f"write_lef_abstract -5.8 -top_layer {tl} -stripe_pins -pg_pin_layers {{{tl}}} {ilm_lef}")
            self.verbose_append(f"write_ilm -model_type all -to_dir {self.ilm_dir_name} -type_flex_ilm ilm")

        for sdc_out in self.output_ilm_sdcs:
            sdc_in = os.path.basename(sdc_out)
            shell_cmd = f'gzip -d -c {self.ilm_dir_name}/mmmc/ilm_data/{self.top_module}/{sdc_in}.gz | sed "s/get_pins/get_pins -hierarchical/g" > {sdc_out}'
            if self.use_python:
                self.py_append(f"import subprocess; subprocess.run({shell_cmd!r}, shell=True, check=True)")
            else:
                self.append(shell_cmd)
        self.ran_write_ilm = True
        return True

    def run_innovus(self) -> bool:
        # Quit Innovus.
        if self.use_python:
            self.py_append("exit()")
        else:
            self.verbose_append("exit")

        # Always write par.tcl for reference / older versions
        par_tcl_filename = os.path.join(self.run_dir, "par.tcl")

        if self.use_python:
            # Output was built natively as Python — write par.py directly
            par_py_filename = os.path.join(self.run_dir, "par.py")
            header = [
                "# " + "=" * 78,
                "# Generated natively for Innovus 25.1 Python API by HAMMER",
                "# " + "=" * 78,
                "",
            ]
            self.write_contents_to_path("\n".join(header + self.output), par_py_filename)
            self.logger.info(f"Generated par.py natively for Innovus {self.version()}")
        else:
            self.write_contents_to_path("\n".join(self.output), par_tcl_filename)

        # Make sure that generated-scripts exists.
        os.makedirs(self.generated_scripts_dir, exist_ok=True)

        # Create open_chip script pointing to latest (symlinked to post_<last ran step>).
        # open_chip.tcl is always plain TCL — never route through self.output/self.append
        # as it will get Python-converted when use_python=True.
        open_chip_tcl_lines = [
            "set_db design_process_node {}".format(self.get_setting("vlsi.core.node")),
            "set_multi_cpu_usage -local_cpu {}".format(self.get_setting("vlsi.core.max_threads")),
            "read_db latest",
        ]
        if self.ran_write_design:
            # Because implementation is done, enable report_timing -early/late and SDF writing
            # without recalculating timing graph for each analysis view
            open_chip_tcl_lines.append("set_db timing_enable_simultaneous_setup_hold_mode true")
        self.write_contents_to_path("\n".join(open_chip_tcl_lines), self.open_chip_tcl)

        with open(self.open_chip_script, "w") as f:
            if self.use_python:  # 25.1+
                f.write("""#!/bin/bash
        cd {run_dir}
        source enter
        export LD_LIBRARY_PATH=/tools/cadence/DDI/DDI251/INNOVUS251/tools.lnx86/cdsgcc/gcc/12.3/install/lib64:$LD_LIBRARY_PATH
        export QT_XCB_GL_INTEGRATION=none
        $INNOVUS_BIN -common_ui -win -files {open_chip_tcl}
                """.format(run_dir=self.run_dir, open_chip_tcl=self.open_chip_tcl))
            else:
                f.write("""#!/bin/bash
        cd {run_dir}
        source enter
        $INNOVUS_BIN -common_ui -win -files {open_chip_tcl}
                """.format(run_dir=self.run_dir, open_chip_tcl=self.open_chip_tcl))
        os.chmod(self.open_chip_script, 0o755)

        # Build args based on version
        if self.use_python:
            args = [
                self.get_setting("par.innovus.innovus_bin"),
                "-nowin",
                "-common_ui",
                "-python",
                "-files", par_py_filename
            ]
        else:
            args = [
                self.get_setting("par.innovus.innovus_bin"),
                "-nowin",
                "-common_ui",
                "-files", par_tcl_filename
            ]

        # Temporarily disable colours/tag to make run output more readable.
        HammerVLSILogging.enable_colour = False
        HammerVLSILogging.enable_tag = False
        self.run_executable(args, cwd=self.run_dir)  # TODO: check for errors and deal with them
        HammerVLSILogging.enable_colour = True
        HammerVLSILogging.enable_tag = True

        # TODO: check that par run was successful

        return True


    def create_floorplan_tcl(self) -> List[str]:
        """
        Create a floorplan TCL depending on the floorplan mode.
        """
        output = []  # type: List[str]

        floorplan_mode = str(self.get_setting("par.innovus.floorplan_mode"))
        if floorplan_mode == "manual":
            floorplan_script_contents = str(self.get_setting("par.innovus.floorplan_script_contents"))
            # TODO(edwardw): proper source locators/SourceInfo
            output.append("# Floorplan manually specified from HAMMER")
            output.extend(floorplan_script_contents.split("\n"))
        elif floorplan_mode == "generate":
            output.extend(self.generate_floorplan_tcl())
        elif floorplan_mode == "auto":
            output.append("# Using auto-generated floorplan")
            output.append("plan_design")
            spacing = self.get_setting("par.blockage_spacing")
            bot_layer = self.get_stackup().get_metal_by_index(1).name
            top_layer = self.get_setting("par.blockage_spacing_top_layer")
            if top_layer is not None:
                output.append("create_place_halo -all_blocks -halo_deltas {{{s} {s} {s} {s}}} -snap_to_site".format(
                    s=spacing))
                output.append("create_route_halo -all_blocks -bottom_layer {b} -space {s} -top_layer {t}".format(
                    b=bot_layer, t=top_layer, s=spacing))
        else:
            if floorplan_mode != "blank":
                self.logger.error("Invalid floorplan_mode {mode}. Using blank floorplan.".format(mode=floorplan_mode))
            # Write blank floorplan
            output.append("# Blank floorplan specified from HAMMER")
        return output

    @staticmethod
    def generate_chip_size_constraint_py(width: Decimal, height: Decimal, left: Decimal, bottom: Decimal,
                                         right: Decimal, top: Decimal, site: str) -> str:
        """Python-native version of generate_chip_size_constraint."""
        return (
            f"create_floorplan(core_margins_by='die', flip='f', "
            f"die_size_by_io_height='max', site={site!r}, "
            f"die_size=[{width}, {height}, {left}, {bottom}, {right}, {top}])"
        )

    def generate_floorplan_py(self) -> List[str]:
        """
        Generate floorplan.py content — Python-native equivalent of generate_floorplan_tcl.
        """
        output = []  # type: List[str]

        chip_size_line = self.generate_chip_size_constraint_py(
            site=self.technology.get_placement_site().name,
            width=Decimal("1000"), height=Decimal("1000"),
            left=Decimal("100"), bottom=Decimal("100"),
            right=Decimal("100"), top=Decimal("100")
        )

        floorplan_constraints = self.get_placement_constraints()
        global_top_layer = self.get_setting("par.blockage_spacing_top_layer")  # type: Optional[str]

        for constraint in floorplan_constraints:
            new_path = "/".join(constraint.path.split("/")[1:])

            if new_path == "":
                assert constraint.type == PlacementConstraintType.TopLevel
                margins = constraint.margins
                assert margins is not None
                chip_size_line = self.generate_chip_size_constraint_py(
                    site=self.technology.get_placement_site().name,
                    width=constraint.width, height=constraint.height,
                    left=margins.left, bottom=margins.bottom,
                    right=margins.right, top=margins.top
                )
            else:
                orientation = constraint.orientation if constraint.orientation is not None else "r0"
                if constraint.create_physical:
                    output.append(
                        f"create_inst(cell={constraint.master!r}, inst={new_path!r}, "
                        f"location=[{constraint.x}, {constraint.y}], orient={orientation!r}, "
                        f"physical=True, status='fixed')"
                    )
                if constraint.type == PlacementConstraintType.Dummy:
                    pass
                elif constraint.type == PlacementConstraintType.SoftPlacement:
                    output.append(
                        f"create_boundary_constraint(type='guide', hinst={new_path!r}, "
                        f"rects=[[{constraint.x}, {constraint.y}, "
                        f"{constraint.x + constraint.width}, {constraint.y + constraint.height}]])"
                    )
                elif constraint.type == PlacementConstraintType.HardPlacement:
                    output.append(
                        f"create_boundary_constraint(type='region', hinst={new_path!r}, "
                        f"rects=[[{constraint.x}, {constraint.y}, "
                        f"{constraint.x + constraint.width}, {constraint.y + constraint.height}]])"
                    )
                elif constraint.type in [PlacementConstraintType.Overlap,
                                         PlacementConstraintType.HardMacro,
                                         PlacementConstraintType.Hierarchical]:
                    fixed_arg = ", fixed=True" if constraint.create_physical else ""
                    output.append(
                        f"place_inst({new_path!r}, {constraint.x}, {constraint.y}, "
                        f"{orientation!r}{fixed_arg})"
                    )
                    if constraint.type in [PlacementConstraintType.HardMacro,
                                           PlacementConstraintType.Hierarchical]:
                        spacing = self.get_setting("par.blockage_spacing")
                        if constraint.top_layer is not None:
                            current_top_layer = constraint.top_layer  # type: Optional[str]
                        elif global_top_layer is not None:
                            current_top_layer = global_top_layer
                        else:
                            current_top_layer = None
                        if current_top_layer is not None:
                            bot_layer = self.get_stackup().get_metal_by_index(1).name
                            cover_layers = list(map(lambda m: m.name,
                                                    self.get_stackup().get_metals_below_layer(current_top_layer)))
                            output.append(
                                f"create_route_halo(bottom_layer={bot_layer!r}, space={spacing}, "
                                f"top_layer={current_top_layer!r}, inst={new_path!r})"
                            )
                            if self.get_setting("par.power_to_route_blockage_ratio") < 1:
                                self.logger.warning("The power strap blockage region is smaller than the routing halo region for hard macros.")
                            place_push_out = round(spacing * self.get_setting("par.power_to_route_blockage_ratio"), 1)
                            output.append(
                                f"create_place_halo(insts={new_path!r}, "
                                f"halo_deltas=[{place_push_out}, {place_push_out}, "
                                f"{place_push_out}, {place_push_out}], snap_to_site=True)"
                            )
                            output.append(
                                f"_pg_blockage_shape = db().get(db().hinsts({new_path!r}) or "
                                f"db().insts({new_path!r})).place_halo_polygon"
                            )
                            output.append(
                                f"create_route_blockage(pg_nets=True, layers={cover_layers!r}, "
                                f"polygon=_pg_blockage_shape)"
                            )
                elif constraint.type == PlacementConstraintType.Obstruction:
                    obs_types = get_or_else(constraint.obs_types, [])
                    assert '/' not in new_path
                    urx = constraint.x + constraint.width
                    ury = constraint.y + constraint.height
                    area_flag = 'rects' if self.version() >= self.version_number("181") else 'area'
                    if ObstructionType.Place in obs_types:
                        output.append(
                            f"create_place_blockage(name={new_path + '_place'!r}, "
                            f"area=[{constraint.x}, {constraint.y}, {urx}, {ury}])"
                        )
                    if ObstructionType.Route in obs_types:
                        layers_arg = (f"layers={get_or_else(constraint.layers, [])!r}"
                                      if constraint.layers is not None else "all_route=True")
                        output.append(
                            f"create_route_blockage(name={new_path + '_route'!r}, "
                            f"except_pg_nets=True, {layers_arg}, spacing=0, "
                            f"{area_flag}=[{constraint.x}, {constraint.y}, {urx}, {ury}])"
                        )
                    if ObstructionType.Power in obs_types:
                        layers_arg = (f"layers={get_or_else(constraint.layers, [])!r}"
                                      if constraint.layers is not None else "all_route=True")
                        output.append(
                            f"create_route_blockage(name={new_path + '_power'!r}, "
                            f"pg_nets=True, {layers_arg}, "
                            f"{area_flag}=[{constraint.x}, {constraint.y}, {urx}, {ury}])"
                        )
                else:
                    assert False, "Should not reach here"

        return [chip_size_line] + output

    def _build_floorplan_py(self) -> List[str]:
        """Build floorplan.py content based on floorplan_mode setting."""
        floorplan_mode = str(self.get_setting("par.innovus.floorplan_mode"))
        lines = ["# Auto-generated floorplan.py for Innovus 25.1", ""]

        if floorplan_mode == "manual":
            # Manual TCL content — convert each line through the converter
            floorplan_script_contents = str(self.get_setting("par.innovus.floorplan_script_contents"))
            lines.append("# Floorplan manually specified from HAMMER")
            lines.extend(InnovusTclToPythonConverter().convert_block(floorplan_script_contents))
        elif floorplan_mode == "generate":
            lines.append("# Generated floorplan from HAMMER constraints")
            lines.extend(self.generate_floorplan_py())
        elif floorplan_mode == "auto":
            lines.append("# Using auto-generated floorplan")
            lines.append("plan_design()")
            spacing = self.get_setting("par.blockage_spacing")
            bot_layer = self.get_stackup().get_metal_by_index(1).name
            top_layer = self.get_setting("par.blockage_spacing_top_layer")
            if top_layer is not None:
                lines.append(
                    f"create_place_halo(all_blocks=True, "
                    f"halo_deltas=[{spacing}, {spacing}, {spacing}, {spacing}], snap_to_site=True)"
                )
                lines.append(
                    f"create_route_halo(all_blocks=True, bottom_layer={bot_layer!r}, "
                    f"space={spacing}, top_layer={top_layer!r})"
                )
        else:
            if floorplan_mode != "blank":
                self.logger.error(f"Invalid floorplan_mode {floorplan_mode}. Using blank floorplan.")
            lines.append("# Blank floorplan specified from HAMMER")

        return lines

    def floorplan_design(self) -> bool:
        floorplan_tcl = os.path.join(self.run_dir, "floorplan.tcl")
        self.write_contents_to_path("\n".join(self.create_floorplan_tcl()), floorplan_tcl)

        if self.use_python:
            floorplan_py = os.path.join(self.run_dir, "floorplan.py")
            self.write_contents_to_path("\n".join(self._build_floorplan_py()), floorplan_py)
            self.py_append(f"exec(open('{floorplan_py}').read())")
        else:
            self.verbose_append(f"source -echo -verbose {floorplan_tcl}")

        # Set "don't use" cells — must happen after floorplan_design (flattened mode)
        if self.hierarchical_mode.is_nonleaf_hierarchical():
            if self.use_python:
                self.py_append("flatten_ilm()")
            else:
                self.verbose_append("flatten_ilm")

        # verbose_append converts for python mode too; the old bespoke branch
        # here emitted read_power_intent(1801=...), which is not legal Python
        # (numeric kwarg). The converter passes numeric flags positionally.
        for l in self.generate_power_spec_commands():
            self.verbose_append(l)

        if self.use_python:
            self._generate_dont_use_commands_python()
        else:
            for l in self.generate_dont_use_commands():
                self.append(l)

        if self.hierarchical_mode.is_nonleaf_hierarchical():
            if self.use_python:
                self.py_append("unflatten_ilm()")
            else:
                self.verbose_append("unflatten_ilm")

        return True

    @staticmethod
    def generate_chip_size_constraint(width: Decimal, height: Decimal, left: Decimal, bottom: Decimal, right: Decimal,
                                      top: Decimal, site: str) -> str:
        """
        Given chip width/height and margins, generate an Innovus TCL command to create the floorplan.
        Also requires a technology specific name for the core site
        """

        site_str = "-site " + site

        # -flip -f allows standard cells to be flipped correctly during place-and-route
        return ("create_floorplan -core_margins_by die -flip f "
                "-die_size_by_io_height max {site_str} "
                "-die_size {{ {width} {height} {left} {bottom} {right} {top} }}").format(
            site_str=site_str,
            width=width,
            height=height,
            left=left,
            bottom=bottom,
            right=right,
            top=top
        )

    def generate_floorplan_tcl(self) -> List[str]:
        """
        Generate a TCL floorplan for Innovus based on the input config/IR.
        Not to be confused with create_floorplan_tcl, which calls this function.
        """
        output = []  # type: List[str]

        # Top-level chip size constraint.
        # Default/fallback constraints if no other constraints are provided.
        # TODO snap this to a core site
        chip_size_constraint = self.generate_chip_size_constraint(
            site=self.technology.get_placement_site().name,
            width=Decimal("1000"), height=Decimal("1000"),
            left=Decimal("100"), bottom=Decimal("100"),
            right=Decimal("100"), top=Decimal("100")
        )

        floorplan_constraints = self.get_placement_constraints()
        global_top_layer = self.get_setting("par.blockage_spacing_top_layer") #  type: Optional[str]

        ############## Actually generate the constraints ################
        for constraint in floorplan_constraints:
            # Floorplan names/insts need to not include the top-level module,
            # despite the internal get_db commands including the top-level module...
            # e.g. Top/foo/bar -> foo/bar
            new_path = "/".join(constraint.path.split("/")[1:])

            if new_path == "":
                assert constraint.type == PlacementConstraintType.TopLevel, "Top must be a top-level/chip size constraint"
                margins = constraint.margins
                assert margins is not None
                # Set top-level chip dimensions.
                chip_size_constraint = self.generate_chip_size_constraint(
                    site=self.technology.get_placement_site().name,
                    width=constraint.width,
                    height=constraint.height,
                    left=margins.left,
                    bottom=margins.bottom,
                    right=margins.right,
                    top=margins.top
                )
            else:
                orientation = constraint.orientation if constraint.orientation is not None else "r0"
                if constraint.create_physical:
                    output.append("create_inst -cell {cell} -inst {inst} -location {{{x} {y}}} -orient {orientation} -physical -status fixed".format(
                        cell=constraint.master,
                        inst=new_path,
                        x=constraint.x,
                        y=constraint.y,
                        orientation=orientation
                    ))
                if constraint.type == PlacementConstraintType.Dummy:
                    pass
                elif constraint.type == PlacementConstraintType.SoftPlacement:
                    output.append("create_boundary_constraint -type guide -hinst {name} -rects {{ {x1} {y1} {x2} {y2} }}".format(
                        name=new_path,
                        x1=constraint.x,
                        x2=constraint.x + constraint.width,
                        y1=constraint.y,
                        y2=constraint.y + constraint.height
                    ))
                elif constraint.type == PlacementConstraintType.HardPlacement:
                    output.append("create_boundary_constraint -type region -hinst {name} -rects {{ {x1} {y1} {x2} {y2} }}".format(
                        name=new_path,
                        x1=constraint.x,
                        x2=constraint.x + constraint.width,
                        y1=constraint.y,
                        y2=constraint.y + constraint.height
                    ))
                elif constraint.type == PlacementConstraintType.Overlap:
                    output.append("place_inst {inst} {x} {y} {orientation}{fixed}".format(
                        inst=new_path,
                        x=constraint.x,
                        y=constraint.y,
                        orientation=orientation,
                        fixed=" -fixed" if constraint.create_physical else ""
                    ))
                elif constraint.type in [PlacementConstraintType.HardMacro, PlacementConstraintType.Hierarchical]:
                    output.append("place_inst {inst} {x} {y} {orientation}{fixed}".format(
                        inst=new_path,
                        x=constraint.x,
                        y=constraint.y,
                        orientation=orientation,
                        fixed=" -fixed" if constraint.create_physical else ""
                    ))
                    spacing = self.get_setting("par.blockage_spacing")
                    if constraint.top_layer is not None:
                        current_top_layer = constraint.top_layer #  type: Optional[str]
                    elif global_top_layer is not None:
                        current_top_layer = global_top_layer
                    else:
                        current_top_layer = None
                    if current_top_layer is not None:
                        bot_layer = self.get_stackup().get_metal_by_index(1).name
                        cover_layers = list(map(lambda m: m.name, self.get_stackup().get_metals_below_layer(current_top_layer)))
                        output.append("create_route_halo -bottom_layer {b} -space {s} -top_layer {t} -inst {inst}".format(
                            inst=new_path, b=bot_layer, t=current_top_layer, s=spacing))

                        if(self.get_setting("par.power_to_route_blockage_ratio") < 1):
                            self.logger.warning("The power strap blockage region is smaller than the routing halo region for hard macros. Double-check if this is intended.")

                        place_push_out = round(spacing*self.get_setting("par.power_to_route_blockage_ratio") , 1) # Push the place halo, and therefore PG blockage, further out from route halo so router is aware of straps before entering final routing.

                        output.append("create_place_halo -insts {inst} -halo_deltas {{{s} {s} {s} {s}}} -snap_to_site".format(
                            inst=new_path, s=place_push_out))
                        output.append("set pg_blockage_shape [get_db [get_db hinsts {inst}][get_db insts {inst}] .place_halo_polygon]".format(
                            inst=new_path))
                        output.append("create_route_blockage -pg_nets -layers {{{layers}}} -polygon $pg_blockage_shape".format(layers=" ".join(cover_layers)))

                elif constraint.type == PlacementConstraintType.Obstruction:
                    obs_types = get_or_else(constraint.obs_types, [])  # type: List[ObstructionType]
                    assert '/' not in new_path, "'obstruction' placement constraints must be provided a path directly under the top level"
                    if ObstructionType.Place in obs_types:
                        output.append("create_place_blockage -name {name}_place -area {{{x} {y} {urx} {ury}}}".format(
                            name=new_path,
                            x=constraint.x,
                            y=constraint.y,
                            urx=constraint.x+constraint.width,
                            ury=constraint.y+constraint.height
                        ))
                    if ObstructionType.Route in obs_types:
                        output.append("create_route_blockage -name {name}_route -except_pg_nets -{layers} -spacing 0 -{area_flag} {{{x} {y} {urx} {ury}}}".format(
                            name=new_path,
                            x=constraint.x,
                            y=constraint.y,
                            urx=constraint.x+constraint.width,
                            ury=constraint.y+constraint.height,
                            area_flag="rects" if self.version() >= self.version_number("181") else "area",
                            layers="all {route}" if constraint.layers is None else f'layers {{{" ".join(get_or_else(constraint.layers, []))}}}'
                        ))
                    if ObstructionType.Power in obs_types:
                        output.append("create_route_blockage -name {name}_power -pg_nets -{layers} -{area_flag} {{{x} {y} {urx} {ury}}}".format(
                            name=new_path,
                            x=constraint.x,
                            y=constraint.y,
                            urx=constraint.x+constraint.width,
                            ury=constraint.y+constraint.height,
                            area_flag="rects" if self.version() >= self.version_number("181") else "area",
                            layers="all {route}" if constraint.layers is None else f'layers {{{" ".join(get_or_else(constraint.layers, []))}}}'
                        ))
                else:
                    assert False, "Should not reach here"
        return [chip_size_constraint] + output

    def specify_std_cell_power_straps(self, blockage_spacing: Decimal, bbox: Optional[List[Decimal]], nets: List[str]) -> List[str]:
        """
        Generate a list of TCL commands that build the low-level standard cell power strap rails.
        This will use the -master option to create power straps based on the tapcells in the special cells list.
        The layer is set by technology.core.std_cell_rail_layer, which should be the highest metal layer in the std cell rails.

        :param bbox: The optional (2N)-point bounding box of the area to generate straps. By default the entire core area is used.
        :param nets: A list of power net names (e.g. ["VDD", "VSS"]). Currently only two are supported.
        :return: A list of TCL commands that will generate power straps on rails.
        """
        layer_name = self.get_setting("technology.core.std_cell_rail_layer")
        layer = self.get_stackup().get_metal(layer_name)
        results = [
            "# Power strap definition for layer {} (rails):\n".format(layer_name),
            "set_db add_stripes_stacked_via_bottom_layer {}".format(layer_name),
            "set_db add_stripes_stacked_via_top_layer {}".format(layer_name),
            "set_db add_stripes_spacing_from_block {}".format(blockage_spacing)
        ]
        tapcells = self.technology.get_special_cell_by_type(CellType.TapCell)[0].name
        options = [
            "-pin_layer", layer_name,
            "-layer", layer_name,
            "-over_pins", "1",
            "-master", "\"{}\"".format(" ".join(tapcells)),
            "-block_ring_bottom_layer_limit", layer_name,
            "-block_ring_top_layer_limit", layer_name,
            "-pad_core_ring_bottom_layer_limit", layer_name,
            "-pad_core_ring_top_layer_limit", layer_name,
            "-direction", str(layer.direction),
            "-width", "pin_width",
            "-nets", "{ %s }" % " ".join(nets)
        ]
        if bbox is not None:
            options.extend([
                "-area", "{ %s }" % " ".join(map(str, bbox))
            ])
        results.append("add_stripes " + " ".join(options) + "\n")
        return results

    def specify_std_cell_power_straps_py(self, blockage_spacing: Decimal, bbox: Optional[List[Decimal]], nets: List[str]) -> List[str]:
        """Python-native version of specify_std_cell_power_straps. Returns Python source lines."""
        layer_name = self.get_setting("technology.core.std_cell_rail_layer")
        self._power_straps_check_index(layer_name)
        tapcells = self.technology.get_special_cell_by_type(CellType.TapCell)[0].name
        results = [
            f"# Power strap definition for layer {layer_name} (rails):",
            f"db().add_stripes_stacked_via_bottom_layer = {layer_name!r}",
            f"db().add_stripes_stacked_via_top_layer = {layer_name!r}",
            f"db().add_stripes_spacing_from_block = {float(blockage_spacing)}",
        ]
        kwargs = {
            'pin_layer': layer_name,
            'layer': layer_name,
            'over_pins': 1,
            'master': " ".join(tapcells),
            'block_ring_bottom_layer_limit': layer_name,
            'block_ring_top_layer_limit': layer_name,
            'pad_core_ring_bottom_layer_limit': layer_name,
            'pad_core_ring_top_layer_limit': layer_name,
            'direction': 'horizontal',
            'width': 'pin_width',
            'nets': nets,
        }
        if bbox is not None:
            kwargs['area'] = list(map(float, bbox))
        else:
            kwargs['area'] = '_CORE_BBOX'  # sentinel replaced below
        args_str = ', '.join(
            f"{k}={v!r}" if v != '_CORE_BBOX'
            else f"{k}=[_core_bbox.ll.x, _core_bbox.ll.y, _core_bbox.ur.x, _core_bbox.ur.y]"
            for k, v in kwargs.items()
        )
        results.append(f"add_stripes({args_str})")
        return results

    def specify_power_straps_py(self, layer_name: str, bottom_via_layer_name: str, blockage_spacing: Decimal, pitch: Decimal, width: Decimal, spacing: Decimal, offset: Decimal, bbox: Optional[List[Decimal]], nets: List[str], add_pins: bool, antenna_trim_shape: str) -> List[str]:
        """Python-native version of specify_power_straps. Returns Python source lines."""
        self._power_straps_check_index(layer_name)
        layer = self.get_stackup().get_metal(layer_name)
        results = [
            f"# Power strap definition for layer {layer_name}:",
            f"db().add_stripes_stacked_via_top_layer = {layer_name!r}",
            f"db().add_stripes_stacked_via_bottom_layer = {bottom_via_layer_name!r}",
            f"db().add_stripes_trim_antenna_back_to_shape = '{{{antenna_trim_shape}}}'",
            f"db().add_stripes_spacing_from_block = {float(blockage_spacing)}",
        ]
        kwargs = {
            'create_pins': 1 if add_pins else 0,
            'block_ring_bottom_layer_limit': layer_name,
            'block_ring_top_layer_limit': bottom_via_layer_name,
            'direction': str(layer.direction),
            'layer': layer_name,
            'nets': nets,
            'pad_core_ring_bottom_layer_limit': bottom_via_layer_name,
            'set_to_set_distance': float(pitch),
            'spacing': float(spacing),
            'switch_layer_over_obs': 0,
            'width': float(width),
        }
        if bbox is not None:
            index = 1 if layer.direction == RoutingDirection.Horizontal else 0
            kwargs['area'] = list(map(float, bbox))
            kwargs['start'] = float(offset + bbox[index])
        else:
            # Use sentinel strings replaced below
            kwargs['area'] = '_CORE_BBOX'
            attr = 'll.y' if layer.direction == RoutingDirection.Horizontal else 'll.x'
            kwargs['start'] = f'_CORE_START_{attr}_{float(offset)}'
        args_str = ', '.join(
            f"{k}={v!r}" if not isinstance(v, str) or not v.startswith('_CORE_')
            else (
                f"{k}=[_core_bbox.ll.x, _core_bbox.ll.y, _core_bbox.ur.x, _core_bbox.ur.y]"
                if v == '_CORE_BBOX'
                else f"{k}=_core_bbox.{v.split('_CORE_START_')[1].rsplit('_', 1)[0]} + {v.rsplit('_', 1)[1]}"
            )
            for k, v in kwargs.items()
        )
        results.append(f"add_stripes({args_str})")
        return results

    def _build_power_straps_py(self) -> List[str]:
        """
        Generate power_straps.py content by re-running the by_tracks logic
        but collecting Python lines instead of TCL lines.
        """
        # Temporarily monkey-patch specify_power_straps and specify_std_cell_power_straps
        # to collect Python lines, then restore them.
        py_lines = ["import os", ""]
        original_specify = self.__class__.specify_power_straps
        original_specify_std = self.__class__.specify_std_cell_power_straps
        original_check = self._power_straps_last_index

        collected = []  # type: List[str]

        def py_specify(self_inner, layer_name, bottom_via_layer_name, blockage_spacing,
                       pitch, width, spacing, offset, bbox, nets, add_pins, antenna_trim_shape):
            lines = self_inner.specify_power_straps_py(
                layer_name, bottom_via_layer_name, blockage_spacing,
                pitch, width, spacing, offset, bbox, nets, add_pins, antenna_trim_shape)
            collected.extend(lines)
            collected.append("")
            return []  # base class extend()s results with our return value

        def py_specify_std(self_inner, blockage_spacing, bbox, nets):
            lines = self_inner.specify_std_cell_power_straps_py(blockage_spacing, bbox, nets)
            collected.extend(lines)
            collected.append("")
            return []

        try:
            self.__class__.specify_power_straps = py_specify
            self.__class__.specify_std_cell_power_straps = py_specify_std
            self._power_straps_last_index = -1
            self.create_power_straps_tcl()  # drives the base class logic, collecting into `collected`
        finally:
            self.__class__.specify_power_straps = original_specify
            self.__class__.specify_std_cell_power_straps = original_specify_std
            self._power_straps_last_index = original_check

        # Prepend _core_bbox if any line references it
        if any('_core_bbox' in l for l in collected):
            py_lines.append("_core_bbox = db().designs()[0].core_bbox")
            py_lines.append("")
        py_lines.extend(collected)
        return py_lines

    def power_straps(self) -> bool:
        """Place the power straps for the design."""
        if self.use_python:
            power_straps_py = os.path.join(self.run_dir, "power_straps.py")
            self.write_contents_to_path("\n".join(self._build_power_straps_py()), power_straps_py)
            self.py_append(f"exec(open('{power_straps_py}').read())")
        else:
            power_straps_tcl = os.path.join(self.run_dir, "power_straps.tcl")
            self.write_contents_to_path("\n".join(self.create_power_straps_tcl()), power_straps_tcl)
            self.verbose_append(f"source -echo -verbose {power_straps_tcl}")
        return True

    def specify_power_straps(self, layer_name: str, bottom_via_layer_name: str, blockage_spacing: Decimal, pitch: Decimal, width: Decimal, spacing: Decimal, offset: Decimal, bbox: Optional[List[Decimal]], nets: List[str], add_pins: bool, antenna_trim_shape: str) -> List[str]:
        """
        Generate a list of TCL commands that will create power straps on a given layer.
        This is a low-level, cad-tool-specific API. It is designed to be called by higher-level methods, so calling this directly is not recommended.
        This method assumes that power straps are built bottom-up, starting with standard cell rails.

        :param layer_name: The layer name of the metal on which to create straps.
        :param bottom_via_layer_name: The layer name of the lowest metal layer down to which to drop vias.
        :param blockage_spacing: The minimum spacing between the end of a strap and the beginning of a macro or blockage.
        :param pitch: The pitch between groups of power straps (i.e. from left edge of strap A to the next left edge of strap A).
        :param width: The width of each strap in a group.
        :param spacing: The spacing between straps in a group.
        :param offset: The offset to start the first group.
        :param bbox: The optional (2N)-point bounding box of the area to generate straps. By default the entire core area is used.
        :param nets: A list of power nets to create (e.g. ["VDD", "VSS"], ["VDDA", "VSS", "VDDB"],  ... etc.).
        :param add_pins: True if pins are desired on this layer; False otherwise.
        :param antenna_trim_shape: Strategy for trimming strap antennae. {none/stripe}
        :return: A list of TCL commands that will generate power straps.
        """
        # TODO check that this has been not been called after a higher-level metal and error if so
        # TODO warn if the straps are off-pitch
        results = ["# Power strap definition for layer %s:\n" % layer_name]
        results.extend([
            "set_db add_stripes_stacked_via_top_layer {}".format(layer_name),
            "set_db add_stripes_stacked_via_bottom_layer {}".format(bottom_via_layer_name),
            "set_db add_stripes_trim_antenna_back_to_shape {{{}}}".format(antenna_trim_shape),
            "set_db add_stripes_spacing_from_block {}".format(blockage_spacing)
        ])
        layer = self.get_stackup().get_metal(layer_name)
        options = [
            "-create_pins", ("1" if (add_pins) else "0"),
            "-block_ring_bottom_layer_limit", layer_name,
            "-block_ring_top_layer_limit", bottom_via_layer_name,
            "-direction", str(layer.direction),
            "-layer", layer_name,
            "-nets", "{%s}" % " ".join(nets),
            "-pad_core_ring_bottom_layer_limit", bottom_via_layer_name,
            "-set_to_set_distance", str(pitch),
            "-spacing", str(spacing),
            "-switch_layer_over_obs", "0",
            "-width", str(width)
        ]
        # Where to get the io-to-core offset from a bbox
        index = 0
        if layer.direction == RoutingDirection.Horizontal:
            index = 1
        elif layer.direction != RoutingDirection.Vertical:
            raise ValueError("Cannot handle routing direction {d} for layer {l} when creating power straps".format(d=str(layer.direction), l=layer_name))

        if bbox is not None:
            options.extend([
                "-area", "{ %s }" % " ".join(map(str, bbox)),
                "-start", str(offset + bbox[index])
            ])
        else:
            # Just put straps in the core area
            options.extend([
                "-area", "[get_db designs .core_bbox]",
                "-start", "[expr [lindex [lindex [get_db designs .core_bbox] 0] {index}] + {offset}]".format(index=index, offset=offset)
            ])
        results.append("add_stripes " + " ".join(options) + "\n")
        return results

def innovus_global_settings(ht: HammerTool) -> bool:
    """Settings that need to be reapplied at every tool invocation"""
    assert isinstance(ht, HammerPlaceAndRouteTool)
    assert isinstance(ht, CadenceTool)
    assert isinstance(ht, Innovus)
    ht.create_enter_script()

    node = ht.get_setting("vlsi.core.node")
    max_threads = ht.get_setting("vlsi.core.max_threads")

    if ht.use_python:
        ht.py_append(f"db().design_process_node = {node!r}")
        ht.py_append(f"set_multi_cpu_usage(local_cpu={max_threads})")
    else:
        ht.verbose_append("set_db design_process_node {}".format(node))
        ht.verbose_append("set_multi_cpu_usage -local_cpu {}".format(max_threads))

    return True

tool = Innovus