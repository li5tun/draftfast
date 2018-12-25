from ortools.linear_solver import pywraplp
from draftfast.settings import OptimizerSettings
from draftfast.dke_exceptions import (InvalidBoundsException,
                                      PlayerBanAndLockException)


class Optimizer(object):
    def __init__(
        self,
        players,
        rule_set,
        settings,
        lineup_constraints,
        exposure_dct
    ):
        settings = settings or OptimizerSettings()
        self.solver = pywraplp.Solver(
            'FD',
            pywraplp.Solver.CBC_MIXED_INTEGER_PROGRAMMING
        )
        self.players = players
        self.enumerated_players = list(enumerate(players))
        self.existing_rosters = settings.existing_rosters or []
        self.salary_min = rule_set.salary_min
        self.salary_max = rule_set.salary_max
        self.roster_size = rule_set.roster_size
        self.position_limits = rule_set.position_limits
        self.offensive_positions = rule_set.offensive_positions
        self.defensive_positions = rule_set.defensive_positions
        self.general_position_limits = rule_set.general_position_limits
        self.showdown = rule_set.game_type == 'showdown'
        self.settings = settings
        self.lineup_constraints = lineup_constraints
        if exposure_dct:
            self.banned_for_exposure = exposure_dct['banned']
            self.locked_for_exposure = exposure_dct['locked']
        else:
            self.banned_for_exposure = []
            self.locked_for_exposure = []

        self.player_to_idx_map = {}
        self.name_to_idx_map = {}
        self.variables = []
        for idx, player in self.enumerated_players:
            self.variables.append(
                self.solver.IntVar(0, 1, player.solver_id)
            )

            self.player_to_idx_map[player.solver_id] = idx
            self.name_to_idx_map[player.name] = idx

            if self._is_locked(player):
                player.lock = True
            if self._is_banned(player):
                player.ban = True

            if player.lock and player.ban:
                raise PlayerBanAndLockException(player.name)

        self.teams = set([p.team for p in self.players])
        self.objective = self.solver.Objective()
        self.objective.SetMaximization()

    def _is_locked(self, p):
        return self.lineup_constraints.is_locked(p.name) or \
               p.name in self.locked_for_exposure or \
               p.lock

    def _is_banned(self, p):
        return self.lineup_constraints.is_banned(p.name) or \
               p.name in self.banned_for_exposure or \
               p.ban

    def solve(self):
        self._set_player_constraints()
        self._set_player_group_constraints()
        self._optimize_on_projected_points()
        self._set_salary_range()
        self._set_roster_size()
        self._set_positions()
        self._set_general_positions()
        self._set_stack()
        self._set_combo()
        self._set_no_duplicate_lineups()
        self._set_min_teams()

        if self.showdown:
            self._set_single_captain()

        self._set_no_opp_defense()


        solution = self.solver.Solve()
        return solution == self.solver.OPTIMAL

    def _set_player_constraints(self):
        multi_constraints = dict()

        for i, p in self.enumerated_players:
            lb = 1 if p.lock else 0
            ub = 0 if p.ban else 1

            if lb > ub:
                raise InvalidBoundsException

            if p.multi_position or (self.showdown and p.captain):
                if p.name not in multi_constraints.keys():
                    multi_constraints[p.name] = self.solver.Constraint(lb, ub)
                constraint = multi_constraints[p.name]
            else:
                constraint = self.solver.Constraint(lb, ub)

            constraint.SetCoefficient(self.variables[i], 1)

    def _set_player_group_constraints(self):
        for group_constraint in self.lineup_constraints:
            if group_constraint.exact:
                lb = ub = group_constraint.exact
            else:
                lb = group_constraint.lb
                ub = group_constraint.ub

            constraint = self.solver.Constraint(lb, ub)
            for name in group_constraint.players:
                idx = self.name_to_idx_map[name]
                constraint.SetCoefficient(self.variables[idx], 1)

    def _optimize_on_projected_points(self):
        for i, player in self.enumerated_players:
            self.objective.SetCoefficient(
                self.variables[i],
                player.proj,
            )

    def _set_salary_range(self):
        salary_cap = self.solver.Constraint(
            self.salary_min,
            self.salary_max,
        )
        for i, player in self.enumerated_players:
            salary_cap.SetCoefficient(
                self.variables[i],
                player.cost
            )

    def _set_roster_size(self):
        size_cap = self.solver.Constraint(
            self.roster_size,
            self.roster_size,
        )

        for variable in self.variables:
            size_cap.SetCoefficient(variable, 1)

    def _set_stack(self):
        if self.settings:
            stacks = self.settings.stacks

            if stacks:
                for stack in stacks:
                    stack_team = stack.team
                    stack_count = stack.count
                    stack_cap = self.solver.Constraint(
                        stack_count,
                        stack_count,
                    )

                    for i, player in self.enumerated_players:
                        if stack_team == player.team:
                            stack_cap.SetCoefficient(
                                self.variables[i],
                                1
                            )

    def _set_combo(self):
        if self.settings:
            combo = self.settings.force_combo
            combo_allow_te = self.settings.combo_allow_te

            combo_skill_type = ['WR']
            if combo_allow_te:
                combo_skill_type.append('TE')

            if combo:
                teams = set([p.team for p in self.players])
                enumerated_players = self.enumerated_players

                for team in teams:
                    skillplayers_on_team = [
                        self.variables[i] for i, p in enumerated_players
                        if p.team == team and p.pos in combo_skill_type
                    ]
                    qbs_on_team = [
                        self.variables[i] for i, p in enumerated_players
                        if p.team == team and p.pos == 'QB'
                    ]
                    self.solver.Add(
                        self.solver.Sum(skillplayers_on_team) >=
                        self.solver.Sum(qbs_on_team)
                    )

    def _set_no_opp_defense(self):
        offensive_pos = self.offensive_positions
        defensive_pos = self.defensive_positions

        use_classic = self.settings.no_offense_against_defense and \
                        not self.showdown
        use_showdown = self.settings.no_defense_against_captain and \
                        self.showdown

        if offensive_pos and defensive_pos and use_classic or use_showdown:
            enumerated_players = self.enumerated_players

            for team in self.teams:
                offensive_against = [
                    self.variables[i] for i, p in enumerated_players
                    if p.pos in offensive_pos and
                    p.is_opposing_team_in_match_up(team) and
                    use_showdown and p.captain
                ]
                defensive = [
                    self.variables[i] for i, p in enumerated_players
                    if p.team == team and p.pos in defensive_pos
                ]

                for p in offensive_against:
                    for d in defensive:
                        self.solver.Add(p <= 1 - d)

    def _set_positions(self):
        for position, min_limit, max_limit in self.position_limits:
            position_cap = self.solver.Constraint(
                min_limit,
                max_limit
            )

            for i, player in self.enumerated_players:
                if position == player.pos:
                    position_cap.SetCoefficient(self.variables[i], 1)

    def _set_general_positions(self):
        for general_position, min_limit, max_limit in \
                self.general_position_limits:
            position_cap = self.solver.Constraint(min_limit, max_limit)

            for i, player in self.enumerated_players:
                if general_position == player.nba_general_position:
                    position_cap.SetCoefficient(
                        self.variables[i],
                        1
                    )

    def _set_no_duplicate_lineups(self):
        for roster in self.existing_rosters:
            max_repeats = self.roster_size - 1
            if self.settings.uniques:
                max_repeats = max(
                    self.roster_size - self.settings.uniques,
                    1
                )
            repeated_players = self.solver.Constraint(
                0,
                max_repeats
            )
            for player in roster.sorted_players():
                i = self.player_to_idx_map.get(player.solver_id)
                if i is not None:
                    repeated_players.SetCoefficient(self.variables[i], 1)

    def _set_min_teams(self):
        teams = []
        for team in self.teams:
            if team:
                constraint = self.solver.IntVar(0, 1, team)
                teams.append(constraint)
                players_on_team = [
                    self.variables[i] for i, p
                    in self.enumerated_players if p.team == team
                ]
                self.solver.Add(constraint <= self.solver.Sum(players_on_team))

        # TODO - add constraint of max players per team per sport
        if len(teams) > 0:
            self.solver.Add(
                self.solver.Sum(teams) >= self.settings.min_teams
            )

    def _set_single_captain(self):
        captain_constraint = self.solver.Constraint(1)
        for i, p in self.enumerated_players:
            if p.captain:
                captain_constraint.SetCoefficient(self.variables[i], 1)
