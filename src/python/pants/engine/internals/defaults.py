# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
"""The `BuildFileDefaultsParserState.set_defaults` is used by the pants.engine.internals.Parser,
exposed as the `__defaults__` BUILD file symbol.

When parsing a BUILD (from the rule `pants.engine.internals.build_files.parse_address_family`) the
defaults from the closest parent BUILD file is passed as input to the parser, and the new defaults
resulting after the BUILD file have been parsed is returned in the `AddressFamily`.

These defaults are then applied when creating the `TargetAdaptor` targets by the `Registrar` in the
parser.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Tuple, Union

from pants.engine.addresses import Address
from pants.engine.target import ImmutableValue, InvalidFieldException, RegisteredTargetTypes
from pants.engine.unions import UnionMembership
from pants.util.frozendict import FrozenDict

SetDefaultsValueT = Mapping[str, Any]
SetDefaultsKeyT = Union[str, Tuple[str, ...]]
SetDefaultsT = Mapping[SetDefaultsKeyT, SetDefaultsValueT]


class BuildFileDefaults(FrozenDict[str, FrozenDict[str, ImmutableValue]]):
    """Map target types to default field values."""


@dataclass
class BuildFileDefaultsParserState:
    address: Address
    defaults: dict[str, Mapping[str, Any]]
    registered_target_types: RegisteredTargetTypes
    union_membership: UnionMembership

    @classmethod
    def create(
        cls,
        path: str,
        defaults: BuildFileDefaults,
        registered_target_types: RegisteredTargetTypes,
        union_membership: UnionMembership,
    ) -> BuildFileDefaultsParserState:
        return cls(
            address=Address(path, generated_name="__defaults__"),
            defaults=dict(defaults),
            registered_target_types=registered_target_types,
            union_membership=union_membership,
        )

    def get_frozen_defaults(self) -> BuildFileDefaults:
        types = self.registered_target_types.aliases_to_types
        return BuildFileDefaults(
            {
                target_alias: FrozenDict(
                    {
                        field_type.alias: field_type.compute_value(
                            raw_value=default, address=self.address
                        )
                        for field_alias, default in fields.items()
                        for field_type in types[target_alias].class_field_types(
                            self.union_membership
                        )
                        if field_alias in (field_type.alias, field_type.deprecated_alias)
                    }
                )
                for target_alias, fields in self.defaults.items()
            }
        )

    def get(self, target_alias: str) -> Mapping[str, Any]:
        # Used by `pants.engine.internals.parser.Parser._generate_symbols.Registrar.__call__`
        return self.defaults.get(target_alias, {})

    def set_defaults(
        self,
        *args: SetDefaultsT,
        all: SetDefaultsValueT | None = None,
        extend: bool = False,
        **kwargs,
    ) -> None:
        defaults: dict[str, dict[str, Any]] = (
            {} if not extend else {k: dict(v) for k, v in self.defaults.items()}
        )

        if all is not None:
            self._process_defaults(
                defaults,
                {tuple(self.registered_target_types.aliases): all},
                ignore_unknown_fields=True,
            )

        for arg in args:
            self._process_defaults(defaults, arg)

        # Update with new defaults, dropping targets without any default values.
        for tgt, default in defaults.items():
            if not default:
                self.defaults.pop(tgt, None)
            else:
                self.defaults[tgt] = default

    def _process_defaults(
        self,
        defaults: dict[str, dict[str, Any]],
        targets_defaults: SetDefaultsT,
        ignore_unknown_fields: bool = False,
    ):
        if not isinstance(targets_defaults, dict):
            raise ValueError(
                f"Expected dictionary mapping targets to default field values for {self.address} "
                f"but got: {type(targets_defaults).__name__}."
            )

        types = self.registered_target_types.aliases_to_types
        for target, default in targets_defaults.items():
            if not isinstance(default, dict):
                raise ValueError(
                    f"Invalid default field values in {self.address} for target type {target}, "
                    f"must be an `dict` but was {default!r} with type `{type(default).__name__}`."
                )

            targets: Iterable[str]
            targets = target if isinstance(target, tuple) else (target,)
            for target_alias in map(str, targets):
                if target_alias in types:
                    target_type = types[target_alias]
                else:
                    raise ValueError(f"Unrecognized target type {target_alias} in {self.address}.")

                # Copy default dict if we may mutate it.
                raw_values = dict(default) if ignore_unknown_fields else default

                # Validate that field exists on target
                valid_field_aliases = set(
                    target_type._get_field_aliases_to_field_types(
                        target_type.class_field_types(self.union_membership)
                    ).keys()
                )

                for field_alias in default.keys():
                    if field_alias not in valid_field_aliases:
                        if ignore_unknown_fields:
                            del raw_values[field_alias]
                        else:
                            raise InvalidFieldException(
                                f"Unrecognized field `{field_alias}` for target {target_type.alias}. "
                                f"Valid fields are: {', '.join(sorted(valid_field_aliases))}.",
                            )

                # TODO: moved fields for TargetGenerators ?  See: `Target._calculate_field_values()`.

                # TODO: support parametrization ? --needs special care due to Parametrize object not
                # being hashable, and thus not acceptable in a FrozenDict instance.

                # Merge all provided defaults for this call.
                defaults.setdefault(target_type.alias, {}).update(raw_values)
