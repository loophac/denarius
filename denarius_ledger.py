import copy

from denarius_protocol import GENESIS_HASH


class ChainState:
    def __init__(
        self,
        balances=None,
        nonces=None,
        confirmed_transactions=None,
        transaction_heights=None,
        immature_rewards=None,
        issued_atomic=0,
        chainwork=0,
        tip_height=0,
        tip_hash=GENESIS_HASH,
    ):
        self.balances = dict(balances or {})
        self.nonces = dict(nonces or {})
        self.confirmed_transactions = set(confirmed_transactions or ())
        self.transaction_heights = dict(transaction_heights or {})
        self.immature_rewards = copy.deepcopy(immature_rewards or [])
        self.issued_atomic = int(issued_atomic)
        self.chainwork = int(chainwork)
        self.tip_height = int(tip_height)
        self.tip_hash = tip_hash

    def clone(self):
        return ChainState(
            balances=self.balances,
            nonces=self.nonces,
            confirmed_transactions=self.confirmed_transactions,
            transaction_heights=self.transaction_heights,
            immature_rewards=self.immature_rewards,
            issued_atomic=self.issued_atomic,
            chainwork=self.chainwork,
            tip_height=self.tip_height,
            tip_hash=self.tip_hash,
        )

    def mature_rewards(self, height):
        remaining = []
        matured = []
        for reward in self.immature_rewards:
            if reward['matures_at'] <= height:
                address = reward['address']
                amount = reward['amount_atomic']
                self.balances[address] = self.balances.get(address, 0) + amount
                matured.append(copy.deepcopy(reward))
            else:
                remaining.append(reward)
        self.immature_rewards = remaining
        return matured

    def immature_balance(self, address):
        return sum(
            reward['amount_atomic']
            for reward in self.immature_rewards
            if reward['address'] == address
        )

    def balance_available_at(self, address, height):
        balance = self.balances.get(address, 0)
        balance += sum(
            reward['amount_atomic']
            for reward in self.immature_rewards
            if reward['address'] == address and reward['matures_at'] <= height
        )
        return balance

    def as_dict(self):
        return {
            'balances': dict(self.balances),
            'nonces': dict(self.nonces),
            'confirmed_transactions': sorted(self.confirmed_transactions),
            'transaction_heights': dict(self.transaction_heights),
            'immature_rewards': copy.deepcopy(self.immature_rewards),
            'issued_atomic': self.issued_atomic,
            'chainwork': self.chainwork,
            'tip_height': self.tip_height,
            'tip_hash': self.tip_hash,
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError('Invalid chain state')
        required = {
            'balances',
            'nonces',
            'confirmed_transactions',
            'transaction_heights',
            'immature_rewards',
            'issued_atomic',
            'chainwork',
            'tip_height',
            'tip_hash',
        }
        if set(value) != required:
            raise ValueError('Invalid chain state')
        return cls(**{
            'balances': value['balances'],
            'nonces': value['nonces'],
            'confirmed_transactions': value['confirmed_transactions'],
            'transaction_heights': value['transaction_heights'],
            'immature_rewards': value['immature_rewards'],
            'issued_atomic': value['issued_atomic'],
            'chainwork': value['chainwork'],
            'tip_height': value['tip_height'],
            'tip_hash': value['tip_hash'],
        })
